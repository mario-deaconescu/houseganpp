import argparse
import os
import numpy as np
import math
import sys
import random

parent_src_path = os.path.abspath(os.path.join(os.path.dirname(__file__)))
if parent_src_path not in sys.path:
    sys.path.insert(0, parent_src_path)

import torchvision.transforms as transforms
from torchvision.utils import save_image
from tqdm import tqdm

from dataset.floorplan_dataset_maps_functional_high_res import FloorplanGraphDataset, floorplan_collate_fn

from torch.utils.data import DataLoader
from torchvision import datasets
from torch.autograd import Variable

import torch.nn as nn
import torch.nn.functional as F
import torch.autograd as autograd
import torch
from PIL import Image, ImageDraw, ImageFont
import svgwrite
from models.models import Generator
# from models.models_improved import Generator

from misc.utils import _init_input, ID_COLOR, draw_masks, draw_graph, estimate_graph, get_device, draw_masks_modified
from collections import defaultdict
import matplotlib.pyplot as plt
import networkx as nx
import glob
import cv2
import webcolors
import time

# optimizers
# Tensor = torch.mps.FloatTensor if torch.mps.is_available() else torch.FloatTensor

# run inference
def _infer(graph, model, prev_state=None):
    # configure input to the network
    z, given_masks_in, given_nds, given_eds = _init_input(graph, prev_state)
    # run inference model
    with torch.no_grad():
        masks = model(z.to(get_device()), given_masks_in.to(get_device()), given_nds.to(get_device()), given_eds.to(get_device()))
        masks = masks.detach().cpu().numpy()
    return masks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--n_cpu", type=int, default=16, help="number of cpu threads to use during batch generation")
    parser.add_argument("--batch_size", type=int, default=1, help="size of the batches")
    parser.add_argument("--checkpoint", type=str, default='./models/houseganpp/pretrained.pth', help="checkpoint path")
    parser.add_argument("--data_path", type=str, default='./data/list_prefixed.txt', help="path to dataset list file")
    parser.add_argument("--out", type=str, default='./outputs/houseganpp', help="output folder")
    parser.add_argument("--num_samples", type=int, default=1000, help="number of samples to generate")
    opt = parser.parse_args()
    print(opt)

    # Create output dir
    os.makedirs(opt.out, exist_ok=True)

    # Initialize generator and discriminator
    model = Generator()
    model.load_state_dict(torch.load(opt.checkpoint, map_location=get_device()), strict=True)
    model = model.eval()

    # Initialize variables
    if torch.mps.is_available():
        model.to(get_device())

    # initialize dataset iterator
    fp_dataset_test = FloorplanGraphDataset(opt.data_path, transforms.Normalize(mean=[0.5], std=[0.5]), split='test')
    fp_loader = torch.utils.data.DataLoader(fp_dataset_test,
                                            batch_size=opt.batch_size,
                                            shuffle=False, collate_fn=floorplan_collate_fn)
    num_generated = 0
    for i, sample in tqdm(enumerate(fp_loader)):

        if num_generated >= opt.num_samples:
            break

        # draw real graph and groundtruth
        mks, nds, eds, _, _ = sample
        real_nodes = np.where(nds.detach().cpu() == 1)[-1]
        graph = [nds, eds]
        # true_graph_obj, graph_im = draw_graph([real_nodes, eds.detach().cpu().numpy()])
        # graph_im.save('./{}/graph_{}.png'.format(opt.out, i)) # save graph

        # add room types incrementally
        _types = sorted(list(set(real_nodes)))
        selected_types = [_types[:k + 1] for k in range(10)]
        os.makedirs('./{}/'.format(opt.out), exist_ok=True)
        _round = 0

        # initialize layout
        state = {'masks': None, 'fixed_nodes': []}
        masks = _infer(graph, model, state)
        # im0 = draw_masks(masks.copy(), real_nodes)
        # im0 = torch.tensor(np.array(im0).transpose((2, 0, 1)))/255.0
        # save_image(im0, './{}/fp_init_{}.png'.format(opt.out, i), nrow=1, normalize=False) # visualize init image

        # generate per room type
        for _iter, _types in enumerate(selected_types):
            _fixed_nds = np.concatenate([np.where(real_nodes == _t)[0] for _t in _types]) \
                if len(_types) > 0 else np.array([])
            state = {'masks': masks, 'fixed_nodes': _fixed_nds}
            masks = _infer(graph, model, state)

        # save final floorplans
        imk = draw_masks_modified(masks.copy(), real_nodes)
        # imk = torch.tensor(np.array(imk).transpose((2, 0, 1)))/255.0
        # save_image(imk, './{}/fp_final_{}.png'.format(opt.out, i), nrow=1, normalize=False)
        cv2.imwrite('./{}/{}.png'.format(opt.out, i), np.array(imk))

        num_generated += 1


if __name__ == '__main__':
    main()