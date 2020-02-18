import numpy as np

import os.path as osp
import os
import argparse
import torch
from torch.nn import Sequential, Linear, ReLU
import torch.nn.functional as F
from torch_scatter import scatter_mean
from torch_geometric.datasets import QM9
import torch_geometric.transforms as T
from torch_geometric.nn import NNConv
from k_gnn import TwoMalkin, ConnectedThreeMalkin


def make_complete_graph(edge_index, edge_features, num_nodes):
    complete_adj = np.ones((num_nodes, num_nodes))
    np.fill_diagonal(complete_adj, 0.)
    comp_edges = np.array([complete_adj.nonzero()[0], complete_adj.nonzero()[1]]).transpose()
    current_edges = edge_index.numpy()
    comp_edge_features = torch.zeros((comp_edges.shape[0], edge_features.shape[1]))

    for i, edge_pair in enumerate(zip(*current_edges)):
        index = np.intersect1d(np.where(edge_pair[0] == comp_edges[:, 0]),
                               np.where(edge_pair[1] == comp_edges[:, 1]))
        comp_edge_features[index, :] = edge_features[i]

    return torch.tensor(comp_edges.transpose()), comp_edge_features




def mycollate(batch):
    data = batch[0]
    keys = set(data.keys)
    n = data.num_nodes
    edge_index = data['edge_index']
    label = data['y']
    node_features = None
    edge_features = None
    if 'x' in keys:
        node_features = torch.cat((data['pos'], data['x']), dim=1)
    if 'edge_attr' in keys:
        edge_features = data['edge_attr']
        comp_edge_index, comp_edge_features = make_complete_graph(edge_index, edge_features, n)
    custom_graph = CustomGraph(comp_edge_index, label, data.num_nodes, comp_edge_features.shape[0], node_feat=node_features,
                               edge_feat=comp_edge_features)
    graph_dict = {
        'incident': custom_graph.i_adj,
        'incident_features': custom_graph.i_features,
        'edge_neigh': custom_graph.edge_neighbors,
        'labels': custom_graph.label
    }

    return graph_dict


class CustomGraph(object):
    def __init__(self, edge_index, label, num_nodes, num_edges, node_feat=None, edge_feat=None):
        self.edge_index = edge_index
        self.label = label
        self.num_nodes = num_nodes
        self.num_edges = num_edges
        self.node_feat = node_feat
        self.edge_feat = edge_feat
        self.edge_neighbors = None
        if not self.node_feat is None:
            self.node_dim = self.node_feat.shape[1]
        if not self.edge_feat is None:
            self.edge_dim = self.edge_feat.shape[1]

        #self.h_adj, self.h_features = self.get_homo_data()
        self.i_adj, self.i_features, self.edge_neighbors = self.get_inhomo_data()

    def get_homo_data(self):
        adjacency = torch.zeros(self.num_nodes, self.num_nodes, dtype=torch.long)
        adjacency[self.edge_index[0], self.edge_index[1]] = 1
        # add self loops
        adjacency[torch.eye(self.num_nodes).byte()] = 1

        r, c = adjacency.nonzero().numpy().transpose()
        diag_idx = np.array(np.where(r == c)[0])
        off_diag_idx = np.array(np.where(r != c)[0])
        if self.node_feat is None:
            if self.edge_feat is None:
                hom_features = torch.zeros((len(r), 2), dtype=torch.float32)
                hom_features[diag_idx, 0] = 1.
                hom_features[off_diag_idx, 1] = 1.
            else:
                hom_features = torch.zeros((len(r), self.edge_dim+1), dtype=torch.float32)
                hom_features[diag_idx, 0] = 1.
                hom_features[off_diag_idx, 1:] = self.edge_feat
        elif self.edge_feat is None:
                hom_features = torch.zeros((len(r), self.node_dim+1), dtype=torch.float32)
                hom_features[diag_idx, 0:self.node_dim] = self.node_feat
                hom_features[off_diag_idx, -1] = 1.
        else:
                hom_features = torch.zeros((len(r), self.node_dim + self.edge_dim), dtype=torch.float32)
                hom_features[diag_idx, 0:self.node_dim] = self.node_feat
                hom_features[off_diag_idx, self.node_dim:] = self.edge_feat

        return adjacency, hom_features

    def get_inhomo_data(self):
        m = int(self.num_edges / 2)
        n = self.num_nodes

        incident = torch.zeros(m, n, dtype=torch.float32)

        edge_pairs = set()

        if self.node_feat is None:
            if self.edge_feat is None:
                incident_features = torch.zeros((m * 2, 1), dtype=torch.float)
            else:
                incident_features = torch.zeros((m * 2, self.edge_dim), dtype=torch.float32)

        elif self.edge_feat is None:
            incident_features = torch.zeros((m * 2, self.node_dim), dtype=torch.float32)
        else:
            incident_features = torch.zeros((m * 2, self.node_dim + self.edge_dim), dtype=torch.float32)

        counter = 0
        edge_counter = 0
        edge_index_array = self.edge_index.numpy()
        edge_neighbors = torch.zeros(m*2)
        for edge in self.edge_index.permute(1, 0):
            if (edge[1].item(), edge[0].item()) in edge_pairs:
                continue
            edge_pairs.add((edge[0].item(), edge[1].item()))
            incident[edge_counter, edge[0]] = 1
            incident[edge_counter, edge[1]] = 1
            edge_counter += 1

            if self.node_feat is None:
                if self.edge_feat is None:
                    #incident_features = torch.zeros((m * 2, 1), dtype=torch.float)
                    incident_features[counter] = 1.
                    counter += 1
                    incident_features[counter] = 1.
                    counter += 1
                else:
                    #incident_features = torch.zeros((m * 2, self.edge_dim), dtype=torch.float32)
                    edge_idx = np.where((edge_index_array[0] == edge[0]) & (edge_index_array[1] == edge[1]))[0][0]
                    incident_features[counter] = self.edge_feat[edge_idx]
                    counter += 1
                    incident_features[counter] = self.edge_feat[edge_idx]
                    counter += 1

            elif self.edge_feat is None:
                #incident_features = torch.zeros((m * 2, self.node_dim), dtype=torch.float32)
                incident_features[counter] = self.node_feat[edge[0]]
                counter += 1
                incident_features[counter] = self.node_feat[edge[1]]
                counter += 1
            else:
                #incident_features = torch.zeros((m * 2, self.node_dim + self.edge_dim), dtype=torch.float32)
                edge_idx = np.where((edge_index_array[0] == edge[0].numpy()) & (edge_index_array[1] == edge[1].numpy()))[0][0]
                incident_features[counter, :self.node_dim] = self.node_feat[edge[0]]
                incident_features[counter, self.node_dim:] = self.edge_feat[edge_idx]
                edge_neighbors[counter] = edge[1]
                counter += 1
                incident_features[counter, :self.node_dim] = self.node_feat[edge[1]]
                incident_features[counter, self.node_dim:] = self.edge_feat[edge_idx]
                edge_neighbors[counter] = edge[0]
                counter += 1

        return incident, incident_features, edge_neighbors



def process_dataset(data_loader, mode='train'):

    molecule_list = []
    print(mode)
    for i, graph_dict in enumerate(data_loader):
        print(i)
        molecule_list.append(graph_dict)
    print('saving file ...')
    directory = '../data/new_qm9_dense_i/'
    if not os.path.exists(directory):
       os.makedirs(directory)
    np.save(directory + mode, molecule_list)



class MyFilter(object):
    def __call__(self, data):
        return data.num_nodes > 6  # Remove graphs with less than 6 nodes.



class MyTransform(object):
    def __call__(self, data):
        data.y = data.y  # Specify target: 0 = mu
        return data


parser = argparse.ArgumentParser()
parser.add_argument('--target', default=0)
args = parser.parse_args()
target = int(args.target)

print('---- Target: {} ----'.format(target))

path = osp.join(osp.dirname(osp.realpath(__file__)), '..', 'data', '1-QM9')
dataset = QM9(path, transform=T.Compose([MyTransform(), T.Distance()]))

dataset = dataset.shuffle()

# Normalize targets to mean = 0 and std = 1.
tenpercent = int(len(dataset) * 0.1)
mean = dataset.data.y[tenpercent:].mean(dim=0)
std = dataset.data.y[tenpercent:].std(dim=0)
dataset.data.y = (dataset.data.y - mean) / std

test_dataset = dataset[:tenpercent]
val_dataset = dataset[tenpercent:2 * tenpercent]
train_dataset = dataset[2 * tenpercent:]

test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=1, collate_fn=mycollate, num_workers=4, pin_memory=True)
val_loader = torch.utils.data.DataLoader(val_dataset, batch_size=1, collate_fn=mycollate, num_workers=4, pin_memory=True)
train_loader = torch.utils.data.DataLoader(train_dataset, shuffle=True, batch_size=1, collate_fn=mycollate,  num_workers=4, pin_memory=True)

process_dataset(train_loader, mode='train')
process_dataset(val_loader, mode='valid')
process_dataset(test_loader, mode='test')
np.save('../data/new_qm9_dense_i/' + 'info', std)
print('done!')



