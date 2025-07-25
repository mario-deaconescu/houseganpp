import argparse
import os
import numpy as np
import math

from floorplan_dataset_maps_functional import FloorplanGraphDataset, floorplan_collate_fn
import torchvision.transforms as transforms
from torchvision.utils import save_image

from torch.utils.data import DataLoader
from torchvision import datasets
from torch.autograd import Variable
import torch.autograd as autograd

import torch.nn as nn
import torch.nn.functional as F
import torch
from PIL import Image, ImageDraw, ImageOps
from utils import combine_images_maps, rectangle_renderer, get_device
from models_exp_3 import Discriminator, Generator, compute_gradient_penalty, weights_init_normal

parser = argparse.ArgumentParser()
parser.add_argument("--n_epochs", type=int, default=100000, help="number of epochs of training")
parser.add_argument("--batch_size", type=int, default=4, help="size of the batches")
parser.add_argument("--g_lr", type=float, default=0.0001, help="adam: learning rate")
parser.add_argument("--d_lr", type=float, default=0.0001, help="adam: learning rate")
parser.add_argument("--b1", type=float, default=0.5, help="adam: decay of first order momentum of gradient")
parser.add_argument("--b2", type=float, default=0.999, help="adam: decay of first order momentum of gradient")
parser.add_argument("--n_cpu", type=int, default=8, help="number of cpu threads to use during batch generation")
parser.add_argument("--latent_dim", type=int, default=128, help="dimensionality of the latent space")
parser.add_argument("--img_size", type=int, default=32, help="size of each image dimension")
parser.add_argument("--sample_interval", type=int, default=50000, help="interval between image sampling")
parser.add_argument("--exp_folder", type=str, default='exp', help="destination folder")
parser.add_argument("--n_critic", type=int, default=1, help="number of training steps for discriminator per iter")
parser.add_argument("--target_set", type=str, default='A', help="which split to remove")
opt = parser.parse_args()

lambda_gp = 10
multi_gpu = False
# exp_folder = "{}_{}_g_lr_{}_d_lr_{}_bs_{}_ims_{}_ld_{}_b1_{}_b2_{}".format(opt.exp_folder, opt.target_set, opt.g_lr, opt.d_lr, \
#                                                                         opt.batch_size, opt.img_size, \
#                                                                         opt.latent_dim, opt.b1, opt.b2)
exp_folder = "{}_{}".format(opt.exp_folder, opt.target_set)
os.makedirs("./exps/"+exp_folder, exist_ok=True)

# Loss function
adversarial_loss = torch.nn.BCEWithLogitsLoss()
distance_loss = torch.nn.L1Loss()

# Initialize generator and discriminator
generator = Generator()
discriminator = Discriminator()

generator.to(get_device())
discriminator.to(get_device())

generator.to(get_device())
discriminator.to(get_device())
adversarial_loss.to(get_device())

# Support to multiple GPUs
def graph_scatter(inputs, device_ids, indices):
    nd_to_sample, ed_to_sample = indices
    batch_size = (torch.max(nd_to_sample) + 1).detach().cpu().numpy()
    N = len(device_ids)
    shift = np.round(np.linspace(0, batch_size, N, endpoint=False)).astype(int)
    shift = list(shift) + [int(batch_size)] 
    outputs = []
    for i in range(len(device_ids)):
        if len(inputs) <= 3:
            x, y, z = inputs
        else:
            x, y, z, w = inputs
        inds = torch.where((nd_to_sample>=shift[i])&(nd_to_sample<shift[i+1]))[0]
        x_split = x[inds]
        y_split = y[inds]
        inds = torch.where(nd_to_sample<shift[i])[0]
        min_val = inds.size(0)      
        inds = torch.where((ed_to_sample>=shift[i])&(ed_to_sample<shift[i+1]))[0]
        z_split = z[inds].clone()
        z_split[:, 0] -= min_val
        z_split[:, 2] -= min_val
        if len(inputs) > 3:
            inds = torch.where((nd_to_sample>=shift[i])&(nd_to_sample<shift[i+1]))[0]
            w_split = (w[inds]-shift[i]).long()            
            _out = (x_split.to(device_ids[i]), \
                    y_split.to(device_ids[i]), \
                    z_split.to(device_ids[i]), \
                    w_split.to(device_ids[i]))
        else:   
            _out = (x_split.to(device_ids[i]), \
                    y_split.to(device_ids[i]), \
                    z_split.to(device_ids[i]))
        outputs.append(_out)
    return outputs

def data_parallel(module, _input, indices):
    device_ids = list(range(torch.cuda.device_count()))
    output_device = device_ids[0]
    replicas = nn.parallel.replicate(module, device_ids)
    inputs = graph_scatter(_input, device_ids, indices)
    replicas = replicas[:len(inputs)]
    outputs = nn.parallel.parallel_apply(replicas, inputs)
    return nn.parallel.gather(outputs, output_device)

# # Initialize weights
# generator.apply(weights_init_normal)
# discriminator.apply(weights_init_normal)

# Select random nodes
def selectRandomNodes(nd_to_sample, batch_size):
    fixed_rooms_num = []
    fixed_nodes = []
    shift = 0
    for k in range(batch_size):
        rooms = np.where(nd_to_sample == k)
        rooms_num = np.array(rooms).shape[-1]
        N = np.random.randint(rooms_num, size=1)
        # select random nodes or all nodes!
        if np.random.normal(0, 1) > 0.5:
            fixed_nodes_state = torch.tensor(np.random.choice(list(range(rooms_num)), size=N, replace=False)).to(get_device()) ##torch.tensor(list(range(rooms_num))).long().to(get_device()) ##
        else:
            fixed_nodes_state = torch.tensor([]).long().to(get_device())
        fixed_nodes_state += shift
        fixed_nodes.append(fixed_nodes_state)
        shift += rooms_num
    fixed_nodes = torch.cat(fixed_nodes)
    bin_fixed_nodes = torch.zeros((nd_to_sample.shape[0], 1))
    bin_fixed_nodes[fixed_nodes] = 1.0
    bin_fixed_nodes = bin_fixed_nodes.float().to(get_device())
    return fixed_nodes, bin_fixed_nodes


# Visualize a single batch
def visualizeSingleBatch(fp_loader_test, opt):
    with torch.no_grad():
        # Unpack batch
        mks, nds, eds, nd_to_sample, ed_to_sample = next(iter(fp_loader_test))
        real_mks = Variable(mks.type(Tensor))
        given_nds = Variable(nds.type(Tensor))
        given_eds = eds
                    
        # Generate a batch of images
        z_shape = [real_mks.shape[0], opt.latent_dim]
        z = Variable(Tensor(np.random.normal(0, 1, tuple(z_shape))))

        # Select random nodes
        ind_fixed_nodes, bin_fixed_nodes = selectRandomNodes(nd_to_sample, 64)

        # Generate a batch of images
        given_masks = torch.tensor(real_mks)
        ind_not_fixed_nodes = torch.tensor([k for k in range(given_masks.shape[0]) if k not in ind_fixed_nodes])
        
        ## Set non fixed masks to -1.0
        given_masks[ind_not_fixed_nodes.long()] = -1.0
        given_masks = given_masks.unsqueeze(1)
        
        ## Add channel to indicate given nodes 
        inds_masks = torch.zeros_like(given_masks)
        inds_masks[ind_not_fixed_nodes.long()] = 0.0
        inds_masks[ind_fixed_nodes.long()] = 1.0
        given_masks = torch.cat([given_masks, inds_masks], 1)

        gen_mks = generator(z, None, given_masks, given_nds, given_eds)
            
        # Generate image tensors
        real_imgs_tensor = combine_images_maps(real_mks, given_nds, given_eds, \
                                               nd_to_sample, ed_to_sample)
        fake_imgs_tensor = combine_images_maps(gen_mks, given_nds, given_eds, \
                                               nd_to_sample, ed_to_sample)

        # Save images
        save_image(real_imgs_tensor, "./exps/{}/{}_real.png".format(exp_folder, batches_done), \
                   nrow=12, normalize=False)
        save_image(fake_imgs_tensor, "./exps/{}/{}_fake.png".format(exp_folder, batches_done), \
                   nrow=12, normalize=False)
    return

# Configure data loader
rooms_path = '/home/nelson/Workspace/autodesk/'
fp_dataset_train = FloorplanGraphDataset(rooms_path, transforms.Normalize(mean=[0.5], std=[0.5]), target_set=opt.target_set)
fp_loader = torch.utils.data.DataLoader(fp_dataset_train, 
                                        batch_size=opt.batch_size, 
                                        shuffle=True,
                                        num_workers=opt.n_cpu,
                                        collate_fn=floorplan_collate_fn)

fp_dataset_test = FloorplanGraphDataset(rooms_path, transforms.Normalize(mean=[0.5], std=[0.5]), target_set=opt.target_set, split='eval')
fp_loader_test = torch.utils.data.DataLoader(fp_dataset_test, 
                                        batch_size=64, 
                                        shuffle=True,
                                        num_workers=opt.n_cpu,
                                        collate_fn=floorplan_collate_fn)

# Optimizers
optimizer_G = torch.optim.Adam(generator.parameters(), lr=opt.g_lr, betas=(opt.b1, opt.b2)) 
optimizer_D = torch.optim.Adam(discriminator.parameters(), lr=opt.d_lr, betas=(opt.b1, opt.b2))
Tensor = torch.cuda.FloatTensor if cuda else torch.mps.FloatTensor if torch.mps.is_available() else torch.FloatTensor
      

# ----------
#  Training
# ----------
batches_done = 0
for epoch in range(opt.n_epochs):
    for i, batch in enumerate(fp_loader):
        
        # Unpack batch
        mks, nds, eds, nd_to_sample, ed_to_sample = batch
        indices = nd_to_sample, ed_to_sample

        # Adversarial ground truths
        batch_size = torch.max(nd_to_sample) + 1
        valid = Variable(Tensor(batch_size, 1)\
                         .fill_(1.0), requires_grad=False)
        fake = Variable(Tensor(batch_size, 1)\
                        .fill_(0.0), requires_grad=False)
    
        # Configure input
        real_mks = Variable(mks.type(Tensor))
        given_nds = Variable(nds.type(Tensor))
        given_eds = eds
        
        # Set grads on
        for p in discriminator.parameters():
            p.requires_grad = True
            
        # ---------------------
        #  Train Discriminator
        # ---------------------
        optimizer_D.zero_grad()

        # Select random nodes
        ind_fixed_nodes, bin_fixed_nodes = selectRandomNodes(nd_to_sample, batch_size)

        # Generate random noise
        z_shape = [real_mks.shape[0], opt.latent_dim]
        z = Variable(Tensor(np.random.normal(0, 1, tuple(z_shape))))

        # Generate a batch of images
        given_masks = torch.tensor(real_mks)
        ind_not_fixed_nodes = torch.tensor([k for k in range(given_masks.shape[0]) if k not in ind_fixed_nodes])
        
        ## Set non fixed masks to -1.0
        given_masks[ind_not_fixed_nodes.long()] = -1.0
        given_masks = given_masks.unsqueeze(1)
        
        ## Add channel to indicate given nodes 
        inds_masks = torch.zeros_like(given_masks)
        inds_masks[ind_not_fixed_nodes.long()] = 0.0
        inds_masks[ind_fixed_nodes.long()] = 1.0
        given_masks = torch.cat([given_masks, inds_masks], 1)
        gen_mks = generator(z, None, given_masks, given_nds, given_eds)

        # Real images
        real_validity = discriminator(real_mks, None, given_nds, given_eds, nd_to_sample)

        # Fake images
        fake_validity = discriminator(gen_mks.detach(), None, given_nds.detach(), \
                                          given_eds.detach(), nd_to_sample.detach())

        # Measure discriminator's ability to classify real from generated samples
        gradient_penalty = compute_gradient_penalty(discriminator, real_mks.data, \
                                                        gen_mks.data, given_nds.data, None, \
                                                        given_eds.data, nd_to_sample.data, \
                                                        None, None)
        d_loss = -torch.mean(real_validity) + torch.mean(fake_validity) \
                 + lambda_gp * gradient_penalty

        # Update discriminator
        d_loss.backward()
        optimizer_D.step()
        
        # -----------------
        #  Train Generator
        # -----------------
        optimizer_G.zero_grad()
        
        # Set grads off
        for p in discriminator.parameters():
            p.requires_grad = False
            
        # Train the generator every n_critic steps
        if i % opt.n_critic == 0:
            
            # Generate a batch of images
            z = Variable(Tensor(np.random.normal(0, 1, tuple(z_shape))))
            gen_mks = generator(z, bin_fixed_nodes, given_masks, given_nds, given_eds)

            # Score fake images
            fake_validity = discriminator(gen_mks, ind_fixed_nodes, given_nds, given_eds, nd_to_sample)

            # Compute L1 loss
            if len(ind_fixed_nodes) > 0:
                err = distance_loss(gen_mks[ind_fixed_nodes, :, :], given_masks[ind_fixed_nodes, 0, :, :]) * 1000
            else:
                err = torch.tensor(0.0)

            # Update generator
            g_loss = -torch.mean(fake_validity) + err 
            g_loss.backward()

            # Update optimizer
            optimizer_G.step()
            print("[Epoch %d/%d] [Batch %d/%d] [D loss: %f] [G loss: %f] [L1 loss: %f]"
                % (epoch, opt.n_epochs, i, len(fp_loader), d_loss.item(), g_loss.item(), err.item()))

            if (batches_done % opt.sample_interval == 0) and batches_done:
                torch.save(generator.state_dict(), './checkpoints/{}_{}.pth'.format(exp_folder, batches_done))
                visualizeSingleBatch(fp_loader_test, opt)
                # exit(0)
            batches_done += opt.n_critic
            
