import torch
from torch.utils.data import Dataset
import h5py
import pickle
import json
import os
from prefetch_generator import BackgroundGenerator
from torch.utils.data import DataLoader, Dataset, sampler
from torchvision import transforms
import albumentations as A
from albumentations.pytorch.transforms import ToTensorV2


# https://www.kaggle.com/ihelon/pytorch-efficientnet-cutout-augmentation
transform_img = A.Compose([
    A.Cutout(num_holes=2, max_h_size=36, max_w_size=128, fill_value=0, p=0.5),
    ToTensorV2(),
], p=1.0)

transform_obj = A.Compose([
    A.Cutout(num_holes=2, max_h_size=32, max_w_size=256, fill_value=0, p=0.5),
    ToTensorV2(),
], p=1.0)
class DataLoaderX(DataLoader):

    def __iter__(self):
        return BackgroundGenerator(super().__iter__())

class CaptionDataset(Dataset):
    """
    A PyTorch Dataset class to be used in a PyTorch DataLoader to create batches.
    """

    def __init__(self, data_folder, data_name, split, two_crop=False, transform=None, scene_graph=False):
        """
        :param data_folder: folder where data files are stored
        :param data_name: base name of processed datasets
        :param split: split, one of 'TRAIN', 'VAL', or 'TEST'
        :param transform: image transform pipeline
        """
        self.split = split
        self.scene_graph = scene_graph
        dataset_name = data_name.split('_')[0]
        self.two_crop = two_crop
        # imgs torch.Size([512, 36, 2048])
        # obj torch.Size([512, 100, 512])
        self.img_height = 36
        self.img_width = 2048
        self.obj_height = 100
        self.obj_width = 512
        # height, width = img.shape[:2]

        assert self.split in {'TRAIN', 'VAL', 'TEST'}

        # Open hdf5 file where images are stored
        if 'gt' not in dataset_name:
            self.sg_train_h5 = h5py.File(data_folder + '/train_scene-graph.hdf5', 'r')
        else:
            self.sg_train_h5 = h5py.File(data_folder + '/train_scene-graph_groundtruth.hdf5', 'r')
        self.train_obj = self.sg_train_h5['object_features']
        self.train_obj_mask = self.sg_train_h5['object_mask']
        self.train_rel = self.sg_train_h5['relation_features']
        self.train_rel_mask = self.sg_train_h5['relation_mask']
        self.train_pair_idx = self.sg_train_h5['relation_pair_idx']
        if 'gt' not in dataset_name:
            self.sg_val_h5 = h5py.File(data_folder + '/val_scene-graph.hdf5', 'r')
        else:
            self.sg_val_h5 = h5py.File(data_folder + '/val_scene-graph_groundtruth.hdf5', 'r')
        self.val_obj = self.sg_val_h5['object_features']
        self.val_obj_mask = self.sg_val_h5['object_mask']
        self.val_rel = self.sg_val_h5['relation_features']
        self.val_rel_mask = self.sg_val_h5['relation_mask']
        self.val_pair_idx = self.sg_val_h5['relation_pair_idx']


        with open(os.path.join(data_folder, self.split + '_SCENE_GRAPHS_FEATURES_' + dataset_name + '.json'), 'r') as j:
            self.sgdet = json.load(j)

        self.train_hf = h5py.File(data_folder + '/train36.hdf5', 'r')
        self.train_features = self.train_hf['image_features']
        self.val_hf = h5py.File(data_folder + '/val36.hdf5', 'r')
        self.val_features = self.val_hf['image_features']

        # Captions per image
        self.cpi = 5

        # Load encoded captions
        with open(os.path.join(data_folder, self.split + '_CAPTIONS_' + data_name + '.json'), 'r') as j:
            self.captions = json.load(j)

        # Load encoded captions
        with open(os.path.join(data_folder, self.split + '_ORIG_CAPTIONS_' + data_name + '.json'), 'r') as j:
            self.orig_captions = json.load(j)

        # Load caption lengths
        with open(os.path.join(data_folder, self.split + '_CAPLENS_' + data_name + '.json'), 'r') as j:
            self.caplens = json.load(j)

        # Load bottom up image features distribution
        with open(os.path.join(data_folder, self.split + '_GENOME_DETS_' + data_name + '.json'), 'r') as j:
            self.objdet = json.load(j)

        # PyTorch transformation pipeline for the image (normalizing, etc.)
        self.transform = transform

        # Total number of datapoints
        self.dataset_size = len(self.captions)

    def __getitem__(self, i):

        # The Nth caption corresponds to the (N // captions_per_image)th image
        objdet = self.objdet[i // self.cpi]
        sgdet = self.sgdet[i // self.cpi]

        caption = torch.tensor(self.captions[i], dtype=torch.long)
        caplen = torch.tensor([self.caplens[i]], dtype=torch.long)

        if sgdet[0] == "v":
            obj = torch.tensor(self.val_obj[sgdet[1]], dtype=torch.float)
            rel = torch.tensor(self.val_rel[sgdet[1]], dtype=torch.float)
            obj_mask = torch.tensor(self.val_obj_mask[sgdet[1]], dtype=torch.bool)
            rel_mask = torch.tensor(self.val_rel_mask[sgdet[1]], dtype=torch.bool)
            pair_idx = self.val_pair_idx[sgdet[1]]
        else:
            obj = torch.tensor(self.train_obj[sgdet[1]], dtype=torch.float)
            rel = torch.tensor(self.train_rel[sgdet[1]], dtype=torch.float)
            obj_mask = torch.tensor(self.train_obj_mask[sgdet[1]], dtype=torch.bool)
            rel_mask = torch.tensor(self.train_rel_mask[sgdet[1]], dtype=torch.bool)
            pair_idx = self.train_pair_idx[sgdet[1]]

        # Load bottom up image features
        if objdet[0] == "v":
            img = torch.tensor(self.val_features[objdet[1]], dtype=torch.float)
        else:
            img = torch.tensor(self.train_features[objdet[1]], dtype=torch.float)

        if self.split == 'TRAIN':
            if self.transform is not None and self.two_crop:
                img1 = torch.squeeze(transform_img(image=img.numpy())['image'], dim=0)
                img2 = torch.squeeze(transform_img(image=img.numpy())['image'], dim=0)
                
                obj1 = torch.squeeze(transform_obj(image=obj.numpy())['image'], dim=0)
                obj2 = torch.squeeze(transform_obj(image=obj.numpy())['image'], dim=0)
                # # img (36, 2048) obj (100, 512)
                return [img1, img2], [obj1, obj2], rel, obj_mask, rel_mask, pair_idx, caption, caplen
            else:
                return img, obj, rel, obj_mask, rel_mask, pair_idx, caption, caplen
        else:
            # For validation of testing, also return all 'captions_per_image' captions to find BLEU-4 score
            all_captions = self.orig_captions[((i // self.cpi) * self.cpi):
                                              (((i // self.cpi) * self.cpi) + self.cpi)]
            return img, obj, rel, obj_mask, rel_mask, pair_idx, caption, caplen, all_captions
            # For validation of testing, also return all 'captions_per_image' captions to find BLEU-4 score

    def __len__(self):
        return self.dataset_size

if __name__ == '__main__':
    # h5py.File(data_folder + '/train36.hdf5', 'r')
    data_folder='/home/chunhui/dataset/mscoco/final_dataset'
    o = h5py.File(data_folder + '/train36.hdf5', 'r')
    # myarray = np.fromfile(data_folder + '/train36.hdf5')
    print('o: ',o['image_features'])
    # print('myarray: ',myarray['image_features'].size())
    # a = pd.read_hdf(data_folder + '/train36.hdf5')
