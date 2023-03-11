import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from .dgcnn import DGCNN
from .utils import random_dilation_encoding,farthest_sampler


class Mish(nn.Module):
    def __init__(self):
        super().__init__()
        print("Mish activation loaded...")

    def forward(self, x):
        x = x * (torch.tanh(F.softplus(x)))
        return x



class Detector(nn.Module):

    def __init__(self, args):
        super(Detector, self).__init__()
        self.ninput = args.npoints
        self.nsample = args.nsample
        self.bs=args.batch_size
        self.k = args.k
        self.nsample=args.nsample
        self.dilation_ratio = args.dilation_ratio
        self.args=args

        self.C1 = 64
        self.C2 = 128
        self.C3 = 256
        self.in_channel = 12

        self.conv1 = nn.Sequential(nn.Conv2d(self.in_channel, self.C1, kernel_size=1, bias=False),
                                   nn.BatchNorm2d(self.C1),
                                   nn.ReLU()
                                   #Mish()
                                   )
        self.conv2 = nn.Sequential(nn.Conv2d(self.C1, self.C2, kernel_size=1, bias=False),
                                   nn.BatchNorm2d(self.C2),
                                   nn.ReLU()
                                   #Mish()
                                    )
        self.conv3 = nn.Sequential(nn.Conv2d(self.C2, self.C3, kernel_size=1, bias=False),
                                   nn.BatchNorm2d(self.C3),
                                   nn.ReLU()
                                   #Mish()
                                    )
        
        self.mlp1 = nn.Sequential(nn.Conv1d(self.C3, self.C3, kernel_size=1),
                                  nn.BatchNorm1d(self.C3),
                                  nn.ReLU())
        self.mlp2 = nn.Sequential(nn.Conv1d(self.C3, self.C3, kernel_size=1),
                                  nn.BatchNorm1d(self.C3),
                                  nn.ReLU())
        self.mlp3 = nn.Sequential(nn.Conv1d(self.C3, 1, kernel_size=1))

        self.softplus = nn.Softplus()
        self.dgcnn = DGCNN(args)

    def begain(self,x):
        all_xyz = x[:, :, :3]
        all_xyz = all_xyz.transpose(2, 1)
        all_xyz = self.dgcnn(all_xyz)
        all_xyz = all_xyz.squeeze(-1)
        all_xyz = all_xyz.transpose(2, 1)
        x = torch.cat((x, all_xyz), dim=-1)  # [B,2*C3,N,k]
        return x



    def forward(self, x):
        if self.args.dataset_name=="kitti":
            random_sample = False
            fs_sample=True
        else:
            x_sample=x[1]#1,3,512
            x=x[0]

        if random_sample:
            # random sample
            randIdx = torch.randperm(self.ninput)[:self.nsample]#
            x_sample = x[:, randIdx, :] #4,512,7
        if fs_sample:
            batch_size,_,W=x.shape
            node_xyz = torch.zeros([batch_size, self.args.nsample,W]).to(x.device)  # batch_size iedian weishu
            x_sample=farthest_sampler(x,self.args.nsample,node_xyz)#
        # random dilation cluster
        random_cluster, random_xyz = random_dilation_encoding(x_sample, x, self.k, self.dilation_ratio)
        # Attentive points aggregation
        bs,channel,n_center,k_nei=random_cluster.shape
        dgcnn_flag=True
        if dgcnn_flag:
            random_cluster_new=random_cluster.permute(0, 2, 1,3).contiguous()
            random_cluster_new=random_cluster_new.view(bs*n_center,channel,k_nei)
            embedding = self.dgcnn(random_cluster_new)#
            embedding=embedding.view(bs,n_center,-1,k_nei)
            embedding = embedding.permute(0, 2, 1, 3).contiguous()
            pass
        else:
            embedding = self.conv3(self.conv2(self.conv1(random_cluster)))#
        x1 = torch.max(embedding, dim=1, keepdim=True)[0]
        x1 = x1.squeeze(dim=1)
        attentive_weights = F.softmax(x1, dim=-1)

        score_xyz = attentive_weights.unsqueeze(1).repeat(1, 3, 1, 1)
        xyz_scored = torch.mul(random_xyz.permute(0, 3, 1, 2).contiguous(), score_xyz)
        keypoints = torch.sum(xyz_scored, dim=-1, keepdim=False)

        score_feature = attentive_weights.unsqueeze(1).repeat(1, self.C3, 1, 1)
        attentive_feature_map = torch.mul(embedding, score_feature)
        global_cluster_feature = torch.sum(attentive_feature_map, dim=-1, keepdim=False)
        saliency_uncertainty = self.mlp3(self.mlp2(self.mlp1(global_cluster_feature)))
        saliency_uncertainty = self.softplus(saliency_uncertainty) + 0.001
        saliency_uncertainty = saliency_uncertainty.squeeze(dim=1)

        return keypoints, saliency_uncertainty, random_cluster, attentive_feature_map

class Descriptor(nn.Module):
    def __init__(self, args):
        super(Descriptor, self).__init__()

        self.C1 = 64
        self.C2 = 128
        self.C3 = 128
        self.C_detector = 256

        self.desc_dim = args.desc_dim
        self.in_channel = 8
        self.k = args.k
        #N C H W
        self.conv1 = nn.Sequential(nn.Conv2d(self.in_channel, self.C1, kernel_size=1, bias=False),
                                   nn.BatchNorm2d(self.C1),
                                   nn.ReLU())
        self.conv2 = nn.Sequential(nn.Conv2d(self.C1, self.C2, kernel_size=1, bias=False),
                                   nn.BatchNorm2d(self.C2),
                                   nn.ReLU())
        self.conv3 = nn.Sequential(nn.Conv2d(self.C2, self.C3, kernel_size=1, bias=False),
                                   nn.BatchNorm2d(self.C3),
                                   nn.ReLU())
        
        self.conv4 = nn.Sequential(nn.Conv2d(2*self.C3+self.C_detector, self.C2, kernel_size=1, bias=False),
                                   nn.BatchNorm2d(self.C2),
                                   nn.ReLU())
        self.conv5 = nn.Sequential(nn.Conv2d(self.C2, self.desc_dim, kernel_size=1, bias=False),
                                   nn.BatchNorm2d(self.desc_dim),
                                   nn.ReLU())
        
    def forward(self, random_cluster, attentive_feature_map):
        x=self.conv1(random_cluster)
        x1 = self.conv3(self.conv2(x))
        x2 = torch.max(x1, dim=3, keepdim=True)[0]
        x2 = x2.repeat(1,1,1,self.k)
        x2 = torch.cat((x2, x1),dim=1) #
        x2 = torch.cat((x2, attentive_feature_map), dim=1)
        x2 = self.conv5(self.conv4(x2))
        desc = torch.max(x2, dim=3, keepdim=False)[0]
        return desc

class GCNKD(nn.Module):

    def __init__(self, args):
        super(GCNKD, self).__init__()

        self.detector = Detector(args)
        self.descriptor = Descriptor(args)
    
    def forward(self, x):
        keypoints, sigmas, random_cluster, attentive_feature_map = self.detector(x)
        #[8,3,128] [8,128] [8,8,128,64],[8,256,128,64] nsample=128,bach_size=4 k=64  [B, 4+C, nsample, k]
        desc = self.descriptor(random_cluster, attentive_feature_map)#[2048,8,32] [4,256,512,32]


        return keypoints, sigmas, desc
