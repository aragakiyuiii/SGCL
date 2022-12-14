import torch
from torch import nn
from torch.nn.utils.weight_norm import weight_norm
from torch.nn.utils.rnn import pad_sequence
import dgl
from utils import create_batched_graphs, create_batched_graphs_augmented

device = torch.device("cuda")

class Attention(nn.Module):
    """
    Attention Network.
    """

    def __init__(self, features_dim, decoder_dim, attention_dim, dropout=0.5):
        """
        :param features_dim: feature size of encoded images
        :param decoder_dim: size of decoder's RNN
        :param attention_dim: size of the attention network
        """
        super(Attention, self).__init__()
        self.features_att = weight_norm(
            nn.Linear(features_dim, attention_dim))  # linear layer to transform encoded image
        self.decoder_att = weight_norm(
            nn.Linear(decoder_dim, attention_dim))  # linear layer to transform decoder's output
        self.full_att = weight_norm(nn.Linear(attention_dim, 1))  # linear layer to calculate values to be softmax-ed
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(p=dropout)
        self.softmax = nn.Softmax(dim=1)  # softmax layer to calculate weights

    def forward(self, image_features, decoder_hidden, mask=None):
        """
        Forward propagation.
        :param image_features: encoded images, a tensor of dimension (batch_size, 36, features_dim)
        :param decoder_hidden: previous decoder output, a tensor of dimension (batch_size, decoder_dim)
        :return: attention weighted encoding, weights
        """
        att1 = self.features_att(image_features)  # (batch_size, N, attention_dim)
        att2 = self.decoder_att(decoder_hidden)  # (batch_size, attention_dim)
        att = self.full_att(self.dropout(self.relu(att1 + att2.unsqueeze(1)))).squeeze(2)  # (batch_size, N)
        if mask is not None:
            # where the mask == 1, fill with value,
            # The mask we receive has ones where an object is, so we inverse it.
            att.masked_fill_(~mask, float('-inf'))
        alpha = self.softmax(att)  # (batch_size, N)
        attention_weighted_encoding = (image_features * alpha.unsqueeze(2)).sum(dim=1)  # (batch_size, features_dim)
        return attention_weighted_encoding


class ContextGAT(nn.Module):
    """
    IO Attention layer, addapted from GAT, very similar to regular attention
    """

    def __init__(self, context_dim, feature_dim, use_obj_info=True, use_rel_info=True, k_update_steps=1,
                 update_relations=False):
        super(ContextGAT, self).__init__()
        self.context_dim = context_dim
        self.feature_dim = feature_dim
        self.use_obj_info = use_obj_info
        self.use_rel_info = use_rel_info
        self.k_update_steps = k_update_steps
        self.update_relations = update_relations
        assert self.use_obj_info or self.use_rel_info, "Either cfg.MODEL.IO.USE_NEIGHBOURHOOD_RELATIONS or " \
                                                       "cfg.MODEL.IO.USE_NEIGHBOURHOOD_RELATIONS must be set to true."
        self.input_proj = nn.Linear(context_dim, feature_dim, bias=False)
        # we always compute the object score, because of the self node
        self.object_score = nn.Linear(feature_dim * 2, 1, bias=False)
        # only have to compute relation score when needed
        if self.use_rel_info or self.update_relations:
            self.relation_score = nn.Linear(feature_dim * 2, 1, bias=False)
        if self.update_relations:
            self.linear_phi_edge = nn.Linear(feature_dim * 2, feature_dim, bias=False)
        self.linear_phi_node = nn.Linear(feature_dim * 2, feature_dim, bias=False)
        self.relu = nn.ReLU()

    def io_attention_send(self, edges):
        # dict for storing messages to the nodes
        mail = dict()

        if self.use_rel_info or self.update_relations:
            s_e = self.relation_score(torch.cat([edges.data['h_t'], edges.data['F_e_t']], dim=-1))
            F_e = edges.data['F_e_t']
            if self.use_rel_info:
                mail['F_e'] = F_e
                mail['s_e'] = s_e
        if self.use_obj_info or self.update_relations:
            # Here edge.src is the data dict from the neighbour nodes
            s_n = edges.src['s_n']
            F_n = edges.src['F_n_t']
            if self.use_obj_info:
                mail['F_n'] = F_n
                mail['s_n'] = s_n
        if self.update_relations:
            # create and compute F_i and s_i, here edges.dst is the destination node or node_self/node_i
            F_i = edges.dst['F_n_t']
            s_i = edges.dst['s_n']
            s = torch.stack([s_n, s_i], dim=1)
            F = torch.stack([F_n, F_i], dim=1)
            alpha_edge = torch.softmax(s, dim=1)
            applied_alpha = torch.sum(alpha_edge * F, dim=1)
            F_e_tplus1 = self.relu(self.linear_phi_edge(torch.cat([applied_alpha, F_e], dim=-1)))
            edges.data['F_e_tplus1'] = F_e_tplus1
        return mail

    def io_attention_reduce(self, nodes):
        # This is executed per node # g.nodes[:]
        s_ne = torch.cat([nodes.mailbox['s_n'], nodes.mailbox['s_e']], dim=-2)
        F_ne = torch.cat([nodes.mailbox['F_n'], nodes.mailbox['F_e']], dim=-2)
        F_i = nodes.data['F_n_t']
        alpha_ne = torch.softmax(s_ne, dim=-2)
        applied_alpha = torch.sum(alpha_ne * F_ne, dim=-2)
        F_i_tplus1 = self.relu(self.linear_phi_node(torch.cat([applied_alpha, F_i], dim=-1)))
        return {'F_i_tplus1': F_i_tplus1}

    def forward(self, input_hidden, graphs: dgl.DGLGraph, batch_num_nodes=None):
        if batch_num_nodes is None:
            b_num_nodes = graphs.batch_num_nodes()
        else:
            b_num_nodes = batch_num_nodes
        h_t = self.input_proj(input_hidden)
        # when there are no edges in the graph, there is nothing to do
        if graphs.number_of_edges() > 0:
            # give all the nodes an edges information about the current querry hidden state
            broadcasted_hn = dgl.broadcast_nodes(graphs, h_t)
            graphs.ndata['h_t'] = broadcasted_hn
            broadcasted_he = dgl.broadcast_edges(graphs, h_t)
            graphs.edata['h_t'] = broadcasted_he
            # create a copy of the node and edge states which will be updated for K iterations
            graphs.ndata['F_n_t'] = graphs.ndata['F_n']
            graphs.edata['F_e_t'] = graphs.edata['F_e']

            for _ in range(self.k_update_steps):
                graphs.ndata['s_n'] = self.object_score(torch.cat([graphs.ndata['h_t'], graphs.ndata['F_n_t']], dim=-1))
                graphs.update_all(self.io_attention_send, self.io_attention_reduce)
                graphs.ndata['F_n_t'] = graphs.ndata['F_i_tplus1']
                if self.update_relations:
                    graphs.edata['F_e_t'] = graphs.edata['F_e_tplus1']

            io = torch.split(graphs.ndata['F_n_t'], split_size_or_sections=b_num_nodes)
        else:
            io = torch.split(graphs.ndata['F_n'], split_size_or_sections=b_num_nodes)
        io = pad_sequence(io, batch_first=True)
        io_mask = io.sum(dim=-1) != 0

        return io, io_mask

class cascade_sg_first_contextGAT_Decoder(nn.Module):
    """
    Decoder.
    """

    def __init__(self, attention_dim, embed_dim, decoder_dim, vocab_size, features_dim=2048,
                 graph_features_dim=512, dropout=0.5, cgat_obj_info=True, cgat_rel_info=True,
                 cgat_k_steps=1, cgat_update_rel=True, augmentation=0, edge_drop_prob=0.2,
                 node_drop_prob=0.2, attr_drop_prob=0.2, predictions_length=16, word_map=None, projection_dim=512, teacher_force=False,embedding_bn=False):
        """
        :param attention_dim: size of attention network
        :param embed_dim: embedding size
        :param decoder_dim: size of decoder's RNN
        :param vocab_size: size of vocabulary
        :param features_dim: feature size of encoded images
        :param dropout: dropout
        """
        super(cascade_sg_first_contextGAT_Decoder, self).__init__()

        self.features_dim = features_dim
        self.attention_dim = attention_dim
        self.embed_dim = embed_dim
        self.decoder_dim = decoder_dim
        self.vocab_size = vocab_size
        self.dropout = dropout

        self.augmentation = augmentation
        self.edge_drop_prob = edge_drop_prob
        self.node_drop_prob = node_drop_prob
        self.attr_drop_prob = attr_drop_prob
        self.predictions_length = predictions_length
        self.word_map = word_map
        # cascade attention network
        self.context_gat = ContextGAT(context_dim=decoder_dim, feature_dim=graph_features_dim,
                                      use_obj_info=cgat_obj_info, use_rel_info=cgat_rel_info,
                                      k_update_steps=cgat_k_steps, update_relations=cgat_update_rel)
        self.cascade1_attention = Attention(graph_features_dim, decoder_dim, attention_dim)
        self.cascade2_attention = Attention(features_dim, decoder_dim + graph_features_dim, attention_dim)

        self.embedding = nn.Embedding(vocab_size, embed_dim)  # embedding layer
        self.dropout = nn.Dropout(p=self.dropout)
        self.top_down_attention = nn.LSTMCell(embed_dim + features_dim + graph_features_dim + decoder_dim,
                                              decoder_dim, bias=True)  # top down attention LSTMCell
        self.language_model = nn.LSTMCell(features_dim + graph_features_dim + decoder_dim, decoder_dim,
                                          bias=True)  # language model LSTMCell
        self.fc1 = weight_norm(nn.Linear(decoder_dim, vocab_size))
        self.fc = weight_norm(nn.Linear(decoder_dim, vocab_size))  # linear layer to find scores over vocabulary
        self.projection_dim = projection_dim
        self.teacher_force = teacher_force
        self.projection = nn.Sequential(nn.Linear(9490, 1024), nn.ReLU(), nn.BatchNorm1d(1024),)
        self.projection1 = nn.Sequential(nn.Linear(1024, projection_dim))
        self.init_weights()  # initialize some layers with the uniform distribution

    def init_weights(self):
        """
        Initializes some parameters with values from the uniform distribution, for easier convergence.
        """
        self.embedding.weight.data.uniform_(-0.1, 0.1)
        self.fc.bias.data.fill_(0)
        self.fc.weight.data.uniform_(-0.1, 0.1)

    def init_hidden_state(self, batch_size):
        """
        Creates the initial hidden and cell states for the decoder's LSTM based on the encoded images.
        :param batch_size: size of the batch
        :return: hidden state, cell state
        """
        h = torch.zeros(batch_size, self.decoder_dim, device="cuda")#.to(device)  # (batch_size, decoder_dim)
        c = torch.zeros(batch_size, self.decoder_dim, device="cuda")#.to(device)
        return h, c

    def forward(self, image_features, object_features, relation_features, object_mask, relation_mask, pair_ids,
                encoded_captions, caption_lengths):
        """
        Forward propagation.
        :param image_features: encoded images, a tensor of dimension (batch_size, enc_image_size, enc_image_size, encoder_dim)
        :param graph_features: encoded images as graphs, a tensor of dimension (batch_size, enc_image_size, enc_image_size, encoder_dim)
        :param graph_mask: mask for the graph_features, shows were non empty features are
        :param encoded_captions: encoded captions, a tensor of dimension (batch_size, max_caption_length)
        :param caption_lengths: caption lengths, a tensor of dimension (batch_size, 1)
        :return: scores for vocabulary, sorted encoded captions, decode lengths, weights, sort indices
        """
        if self.teacher_force:
            return self._forward_with_teacher_forcing(image_features, object_features, relation_features, object_mask, relation_mask, pair_ids, encoded_captions, caption_lengths)
        else:
            return self._forward_no_teacher_force(image_features, object_features, relation_features, object_mask, relation_mask, pair_ids, encoded_captions, caption_lengths)

    def _forward_with_teacher_forcing(self, image_features, object_features, relation_features, object_mask, relation_mask, pair_ids,
                encoded_captions, caption_lengths):
        """
        Forward propagation.
        :param image_features: encoded images, a tensor of dimension (batch_size, enc_image_size, enc_image_size, encoder_dim)
        :param graph_features: encoded images as graphs, a tensor of dimension (batch_size, enc_image_size, enc_image_size, encoder_dim)
        :param graph_mask: mask for the graph_features, shows were non empty features are
        :param encoded_captions: encoded captions, a tensor of dimension (batch_size, max_caption_length)
        :param caption_lengths: caption lengths, a tensor of dimension (batch_size, 1)
        :return: scores for vocabulary, sorted encoded captions, decode lengths, weights, sort indices
        """
        batch_size = image_features.size(0)
        vocab_size = self.vocab_size
    
        # Flatten image
        image_features_mean = image_features.mean(1).to(device)  # (batch_size, num_pixels, encoder_dim)
        # print('object_features.device',object_features.device,' relation_features.device',relation_features.device, 'object_mask.device', object_mask.device, 'relation_mask.device', relation_mask.device)
        graph_features_mean = torch.cat([object_features, relation_features], dim=1).sum(dim=1) / \
                              torch.cat([object_mask, relation_mask], dim=1).sum(dim=1, keepdim=True)
        graph_features_mean = graph_features_mean.to(device)
    
        # Sort input data by decreasing lengths; why? apparent below
        caption_lengths, sort_ind = caption_lengths.squeeze(1).sort(dim=0, descending=True)
        image_features = image_features[sort_ind]
        object_features = object_features[sort_ind]
        object_mask = object_mask[sort_ind]
        relation_features = relation_features[sort_ind]
        relation_mask = relation_mask[sort_ind]
        pair_ids = pair_ids[sort_ind]
        image_features_mean = image_features_mean[sort_ind]
        graph_features_mean = graph_features_mean[sort_ind]
        encoded_captions = encoded_captions[sort_ind]
    
        # initialize the graphs
        if self.training:
            g, object_features, object_mask = create_batched_graphs_augmented(object_features, object_mask,
                                                                              relation_features, relation_mask,
                                                                              pair_ids,
                                                                              augmentation=self.augmentation,
                                                                              edge_drop_prob=self.edge_drop_prob,
                                                                              node_drop_prob=self.node_drop_prob,
                                                                              attr_drop_prob=self.attr_drop_prob)
        else:
            g, object_features, object_mask = create_batched_graphs_augmented(object_features, object_mask,
                                                                              relation_features, relation_mask,
                                                                              pair_ids,
                                                                              augmentation=0,
                                                                              edge_drop_prob=self.edge_drop_prob,
                                                                              node_drop_prob=self.node_drop_prob,
                                                                              attr_drop_prob=self.attr_drop_prob)
        # Embedding
        embeddings = self.embedding(encoded_captions)  # (batch_size, max_caption_length, embed_dim)
        # Initialize LSTM state
        h1, c1 = self.init_hidden_state(batch_size)  # (batch_size, decoder_dim)
        h2, c2 = self.init_hidden_state(batch_size)  # (batch_size, decoder_dim)
    
        # We won't decode at the <end> position, since we've finished generating as soon as we generate <end>
        # So, decoding lengths are actual lengths - 1
        decode_lengths = (caption_lengths - 1).tolist()
    
        # Create tensors to hold word predicion scores
        predictions = torch.zeros(batch_size, max(decode_lengths), vocab_size, device="cuda")
        predictions1 = torch.zeros(batch_size, max(decode_lengths), vocab_size, device="cuda")
    
        # At each time-step, pass the language model's previous hidden state, the mean pooled bottom up features and
        # word embeddings to the top down attention model. Then pass the hidden state of the top down model and the bottom up
        # features to the attention block. The attention weighed bottom up features and hidden state of the top down attention model
        # are then passed to the language model
        for t in range(max(decode_lengths)):
            batch_size_t = sum([l > t for l in decode_lengths])
            # print('g:', g)
            sub_g = dgl.batch(g[:batch_size_t])
            h1, c1 = self.top_down_attention(torch.cat([h2[:batch_size_t],
                                                        image_features_mean[:batch_size_t],
                                                        graph_features_mean[:batch_size_t],
                                                        embeddings[:batch_size_t, t, :]], dim=1),
                                             (h1[:batch_size_t], c1[:batch_size_t]))
            cgat_out, cgat_mask_out = self.context_gat(h1[:batch_size_t], sub_g,
                                                       batch_num_nodes=sub_g.batch_num_nodes().tolist())
            # make sure the size doesn't decrease
            of = object_features[:batch_size_t]
            om = object_mask[:batch_size_t]
            cgat_obj = torch.zeros_like(of)  # size of number of objects
            cgat_obj[:, :cgat_out.size(1)] = cgat_out  # fill with output of cgat
            cgat_mask = torch.zeros_like(om)  # mask shaped like original objects
            cgat_mask[:, :cgat_mask_out.size(1)] = cgat_mask_out  # copy over mask from cgat
            cgat_obj[~cgat_mask & om] = of[~cgat_mask & om]  # fill the no in_degree nodes with the original state
            # we pass the object mask. We used the cgat_mask only to determine which io's where filled and which not.
            graph_weighted_enc = self.cascade1_attention(cgat_obj[:batch_size_t], h1[:batch_size_t], mask=om)
            img_weighted_enc = self.cascade2_attention(image_features[:batch_size_t],
                                                       torch.cat([h1[:batch_size_t], graph_weighted_enc[:batch_size_t]],
                                                                 dim=1))
            preds1 = self.fc1(self.dropout(h1))
        
            h2, c2 = self.language_model(
                torch.cat([graph_weighted_enc[:batch_size_t], img_weighted_enc[:batch_size_t], h1[:batch_size_t]],
                          dim=1),
                (h2[:batch_size_t], c2[:batch_size_t]))
            preds = self.fc(self.dropout(h2))  # (batch_size_t, vocab_size)
            predictions[:batch_size_t, t, :] = preds
            predictions1[:batch_size_t, t, :] = preds1
        return predictions, predictions1, encoded_captions, decode_lengths, sort_ind
    
    def _forward_no_teacher_force(self, image_features, object_features, relation_features, object_mask, relation_mask, pair_ids,
                encoded_captions, caption_lengths):
        """
        Forward propagation.
        :param image_features: encoded images, a tensor of dimension (batch_size, enc_image_size, enc_image_size, encoder_dim)
        :param graph_features: encoded images as graphs, a tensor of dimension (batch_size, enc_image_size, enc_image_size, encoder_dim)
        :param graph_mask: mask for the graph_features, shows were non empty features are
        :param encoded_captions: encoded captions, a tensor of dimension (batch_size, max_caption_length)
        :param caption_lengths: caption lengths, a tensor of dimension (batch_size, 1)
        :return: scores for vocabulary, sorted encoded captions, decode lengths, weights, sort indices
        """
        batch_size = image_features.size(0)
        vocab_size = self.vocab_size
    
        # Flatten image
        image_features_mean = image_features.mean(1).to(device)  # (batch_size, num_pixels, encoder_dim)
        graph_features_mean = torch.cat([object_features, relation_features], dim=1).sum(dim=1) / \
                              torch.cat([object_mask, relation_mask], dim=1).sum(dim=1, keepdim=True)
        graph_features_mean = graph_features_mean.to(device)
    
        # Sort input data by decreasing lengths; why? apparent below
        caption_lengths, sort_ind = caption_lengths.squeeze(1).sort(dim=0, descending=True)
        image_features = image_features[sort_ind]
        object_features = object_features[sort_ind]
        object_mask = object_mask[sort_ind]
        relation_features = relation_features[sort_ind]
        relation_mask = relation_mask[sort_ind]
        pair_ids = pair_ids[sort_ind]
        image_features_mean = image_features_mean[sort_ind]
        graph_features_mean = graph_features_mean[sort_ind]
    
        # initialize the graphs
        if self.training:
            g, object_features, object_mask = create_batched_graphs_augmented(object_features, object_mask,
                                                                              relation_features, relation_mask,
                                                                              pair_ids,
                                                                              augmentation=self.augmentation,
                                                                              edge_drop_prob=self.edge_drop_prob,
                                                                              node_drop_prob=self.node_drop_prob,
                                                                              attr_drop_prob=self.attr_drop_prob)
        else:
            g, object_features, object_mask = create_batched_graphs_augmented(object_features, object_mask,
                                                                              relation_features, relation_mask,
                                                                              pair_ids,
                                                                              augmentation=0,
                                                                              edge_drop_prob=self.edge_drop_prob,
                                                                              node_drop_prob=self.node_drop_prob,
                                                                              attr_drop_prob=self.attr_drop_prob)
        
        # Tensor to store top k previous words at each step; now they're just <start>
        k_prev_words = torch.tensor([[self.word_map['<start>']]] * batch_size, dtype=torch.long).to(device)  # (k, 1)

        # Embedding
        embeddings = torch.squeeze(self.embedding(k_prev_words),1)  # (batch_size, max_caption_length, embed_dim)
        # Initialize LSTM state
        h1, c1 = self.init_hidden_state(batch_size)  # (batch_size, decoder_dim)
        h2, c2 = self.init_hidden_state(batch_size)  # (batch_size, decoder_dim)
    
        # We won't decode at the <end> position, since we've finished generating as soon as we generate <end>
        # So, decoding lengths are actual lengths - 1
        decode_lengths = (caption_lengths - 1).tolist()
    
        # Create tensors to hold word predicion scores
        predictions = torch.zeros(batch_size, max(decode_lengths), vocab_size, device="cuda")
        predictions1 = torch.zeros(batch_size, max(decode_lengths), vocab_size, device="cuda")
    
        # At each time-step, pass the language model's previous hidden state, the mean pooled bottom up features and
        # word embeddings to the top down attention model. Then pass the hidden state of the top down model and the bottom up
        # features to the attention block. The attention weighed bottom up features and hidden state of the top down attention model
        # are then passed to the language model
        for t in range(max(decode_lengths)):
            batch_size_t = sum([l > t for l in decode_lengths])
            sub_g = dgl.batch(g[:batch_size_t])
            h1, c1 = self.top_down_attention(torch.cat([h2[:batch_size_t],
                                                        image_features_mean[:batch_size_t],
                                                        graph_features_mean[:batch_size_t],
                                                        embeddings[:batch_size_t]], dim=1),
                                             (h1[:batch_size_t], c1[:batch_size_t]))
            cgat_out, cgat_mask_out = self.context_gat(h1[:batch_size_t], sub_g,
                                                       batch_num_nodes=sub_g.batch_num_nodes().tolist())
            # make sure the size doesn't decrease
            of = object_features[:batch_size_t]
            om = object_mask[:batch_size_t]
            cgat_obj = torch.zeros_like(of)  # size of number of objects
            cgat_obj[:, :cgat_out.size(1)] = cgat_out  # fill with output of cgat
            cgat_mask = torch.zeros_like(om)  # mask shaped like original objects
            cgat_mask[:, :cgat_mask_out.size(1)] = cgat_mask_out  # copy over mask from cgat
            cgat_obj[~cgat_mask & om] = of[~cgat_mask & om]  # fill the no in_degree nodes with the original state
            # we pass the object mask. We used the cgat_mask only to determine which io's where filled and which not.
            graph_weighted_enc = self.cascade1_attention(cgat_obj[:batch_size_t], h1[:batch_size_t], mask=om)
            img_weighted_enc = self.cascade2_attention(image_features[:batch_size_t],
                                                       torch.cat([h1[:batch_size_t], graph_weighted_enc[:batch_size_t]],
                                                                 dim=1))
            preds1 = self.fc1(self.dropout(h1))
        
            h2, c2 = self.language_model(
                torch.cat([graph_weighted_enc[:batch_size_t], img_weighted_enc[:batch_size_t], h1[:batch_size_t]],
                          dim=1),
                (h2[:batch_size_t], c2[:batch_size_t]))
            preds = self.fc(self.dropout(h2))  # (batch_size_t, vocab_size)
            predictions[:batch_size_t, t, :] = preds
            predictions1[:batch_size_t, t, :] = preds1

            embeddings = self.embedding(preds.max(1)[1])  # predicted.shape=(batch_size, time step=1)
        return predictions, predictions1, encoded_captions, decode_lengths, sort_ind

    def _schedule_sampleing(self, image_features, object_features, relation_features, object_mask, relation_mask, pair_ids, encoded_captions, caption_lengths):
        """
        Forward propagation.
        :param image_features: encoded images, a tensor of dimension (batch_size, enc_image_size, enc_image_size, encoder_dim)
        :param graph_features: encoded images as graphs, a tensor of dimension (batch_size, enc_image_size, enc_image_size, encoder_dim)
        :param graph_mask: mask for the graph_features, shows were non empty features are
        :param encoded_captions: encoded captions, a tensor of dimension (batch_size, max_caption_length)
        :param caption_lengths: caption lengths, a tensor of dimension (batch_size, 1)
        :return: scores for vocabulary, sorted encoded captions, decode lengths, weights, sort indices
        """
        batch_size = image_features.size(0)
        vocab_size = self.vocab_size
    
        # Flatten image
        image_features_mean = image_features.mean(1).to(device)  # (batch_size, num_pixels, encoder_dim)
        graph_features_mean = torch.cat([object_features, relation_features], dim=1).sum(dim=1) / \
                              torch.cat([object_mask, relation_mask], dim=1).sum(dim=1, keepdim=True)
        graph_features_mean = graph_features_mean.to(device)
    
        # Sort input data by decreasing lengths; why? apparent below
        caption_lengths, sort_ind = caption_lengths.squeeze(1).sort(dim=0, descending=True)
        image_features = image_features[sort_ind]
        object_features = object_features[sort_ind]
        object_mask = object_mask[sort_ind]
        relation_features = relation_features[sort_ind]
        relation_mask = relation_mask[sort_ind]
        pair_ids = pair_ids[sort_ind]
        image_features_mean = image_features_mean[sort_ind]
        graph_features_mean = graph_features_mean[sort_ind]
    
        # initialize the graphs
        if self.training:
            g, object_features, object_mask = create_batched_graphs_augmented(object_features, object_mask,
                                                                              relation_features, relation_mask,
                                                                              pair_ids,
                                                                              augmentation=self.augmentation,
                                                                              edge_drop_prob=self.edge_drop_prob,
                                                                              node_drop_prob=self.node_drop_prob,
                                                                              attr_drop_prob=self.attr_drop_prob)
        else:
            g, object_features, object_mask = create_batched_graphs_augmented(object_features, object_mask,
                                                                              relation_features, relation_mask,
                                                                              pair_ids,
                                                                              augmentation=0,
                                                                              edge_drop_prob=self.edge_drop_prob,
                                                                              node_drop_prob=self.node_drop_prob,
                                                                              attr_drop_prob=self.attr_drop_prob)
    
        # Tensor to store top k previous words at each step; now they're just <start>
        k_prev_words = torch.tensor([[self.word_map['<start>']]] * batch_size, dtype=torch.long).to(device)  # (k, 1)
    
        # Embedding
        embeddings = self.embedding(encoded_captions)  # (batch_size, max_caption_length, embed_dim)
        teacher_force_embeddings = embeddings
        embeddings = teacher_force_embeddings
        # Initialize LSTM state
        h1, c1 = self.init_hidden_state(batch_size)  # (batch_size, decoder_dim)
        h2, c2 = self.init_hidden_state(batch_size)  # (batch_size, decoder_dim)
    
        # We won't decode at the <end> position, since we've finished generating as soon as we generate <end>
        # So, decoding lengths are actual lengths - 1
        decode_lengths = (caption_lengths - 1).tolist()
    
        # Create tensors to hold word predicion scores
        predictions = torch.zeros(batch_size, max(decode_lengths), vocab_size, device="cuda")
        predictions1 = torch.zeros(batch_size, max(decode_lengths), vocab_size, device="cuda")

        # At each time-step, pass the language model's previous hidden state, the mean pooled bottom up features and
        # word embeddings to the top down attention model. Then pass the hidden state of the top down model and the bottom up
        # features to the attention block. The attention weighed bottom up features and hidden state of the top down attention model
        # are then passed to the language model
        for t in range(max(decode_lengths)):
            batch_size_t = sum([l > t for l in decode_lengths])
            sub_g = dgl.batch(g[:batch_size_t])
            if t==0 or sampled_teacher_force:
                h1, c1 = self.top_down_attention(torch.cat([h2[:batch_size_t],
                                                            image_features_mean[:batch_size_t],
                                                            graph_features_mean[:batch_size_t],
                                                            embeddings[:batch_size_t, t, :]], dim=1),
                                                 (h1[:batch_size_t], c1[:batch_size_t]))
            else:
                h1, c1 = self.top_down_attention(torch.cat([h2[:batch_size_t],
                                                        image_features_mean[:batch_size_t],
                                                        graph_features_mean[:batch_size_t],
                                                        embeddings[:batch_size_t]], dim=1),
                                             (h1[:batch_size_t], c1[:batch_size_t]))
            cgat_out, cgat_mask_out = self.context_gat(h1[:batch_size_t], sub_g,
                                                       batch_num_nodes=sub_g.batch_num_nodes().tolist())
            # make sure the size doesn't decrease
            of = object_features[:batch_size_t]
            om = object_mask[:batch_size_t]
            cgat_obj = torch.zeros_like(of)  # size of number of objects
            cgat_obj[:, :cgat_out.size(1)] = cgat_out  # fill with output of cgat
            cgat_mask = torch.zeros_like(om)  # mask shaped like original objects
            cgat_mask[:, :cgat_mask_out.size(1)] = cgat_mask_out  # copy over mask from cgat
            cgat_obj[~cgat_mask & om] = of[~cgat_mask & om]  # fill the no in_degree nodes with the original state
            # we pass the object mask. We used the cgat_mask only to determine which io's where filled and which not.
            graph_weighted_enc = self.cascade1_attention(cgat_obj[:batch_size_t], h1[:batch_size_t], mask=om)
            img_weighted_enc = self.cascade2_attention(image_features[:batch_size_t],
                                                       torch.cat([h1[:batch_size_t], graph_weighted_enc[:batch_size_t]],
                                                                 dim=1))
            preds1 = self.fc1(self.dropout(h1))
        
            h2, c2 = self.language_model(
                torch.cat([graph_weighted_enc[:batch_size_t], img_weighted_enc[:batch_size_t], h1[:batch_size_t]],
                          dim=1),
                (h2[:batch_size_t], c2[:batch_size_t]))
            preds = self.fc(self.dropout(h2))  # (batch_size_t, vocab_size)
            predictions[:batch_size_t, t, :] = preds
            predictions1[:batch_size_t, t, :] = preds1
            if 1:#sampling_rate:
                # do not use force
                embeddings = teacher_force_embeddings
                sampled_teacher_force = True
            else:
                # use force
                embeddings = self.embedding(preds.max(1)[1])  # predicted.shape=(batch_size, time step=1)
                sampled_teacher_force = False
        return predictions, predictions1, encoded_captions, decode_lengths, sort_ind