import argparse
import yaml
import time

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec
import matplotlib as mpl

import torch
from torch.utils.data import TensorDataset, random_split, DataLoader
from torch.nn.functional import pad

from sklearn.model_selection import StratifiedKFold

import librosa
import librosa.display

from . import core
from . import data
from . import viz

parser = argparse.ArgumentParser() 
subargs = parser.add_subparsers(prog='wavetorch', title="commands", dest="command") 

# Global options
args_global = argparse.ArgumentParser(add_help=False)
args_global.add_argument('--num_threads', type=int, default=4,
                            help='Number of threads to use')
args_global.add_argument('--use-cuda', action='store_true',
                            help='Use CUDA to perform computations')

### Training mode
args_train = subargs.add_parser('train', parents=[args_global])
args_train.add_argument('--config', type=str, required=True,
                            help='Config file to use')
args_train.add_argument('--name', type=str, default=None,
                            help='Name to use when saving or loading the model file. If not specified when saving a time and date stamp is used')
args_train.add_argument('--savedir', type=str, default='./study/',
                            help='Directory in which the model file is saved. Defaults to ./study/')
###

### Cross validation mode
args_cross = subargs.add_parser('cross', parents=[args_global])
args_cross.add_argument('--config', type=str, required=True,
                            help='Config file to use')
args_cross.add_argument('--n_splits', type=int, default=3,
                            help='Number of folds')
###

### Summary mode
args_summary = subargs.add_parser('summary', parents=[args_global])
args_summary.add_argument('model_file')

### Inference mode
args_inference = subargs.add_parser('inference', parents=[args_global])
args_inference.add_argument('--fields', action='store_true',
                            help='Plot the integrated field distrubtion')
args_inference.add_argument('--stft', action='store_true',
                            help='Plot the STFTs')
args_inference.add_argument('--animate', action='store_true',
                            help='Animate the field for the vowel classes')
args_inference.add_argument('--save', action='store_true',
                            help='Save figures')
###

class WaveTorch(object):

    def __init__(self):
        args = parser.parse_args()

        if args.use_cuda and torch.cuda.is_available():
            args.dev = torch.device('cuda')
        else:
            args.dev = torch.device('cpu')

        torch.set_num_threads(args.num_threads)

        if not hasattr(self, args.command):
            print('Unrecognized command')
            parser.print_help()
            exit(1)

        getattr(self, args.command)(args)

    def train(self, args):
        print("Using configuration from %s: " % args.config)
        with open(args.config, 'r') as ymlfile:
             cfg = yaml.load(ymlfile)
             print(yaml.dump(cfg, default_flow_style=False))

        N_classes = len(cfg['data']['vowels'])

        x_train, x_test, y_train, y_test = data.load_selected_vowels(
                                                cfg['data']['vowels'],
                                                gender=cfg['data']['gender'], 
                                                sr=cfg['data']['sr'], 
                                                normalize=True, 
                                                train_size=cfg['training']['train_size'], 
                                                test_size=cfg['training']['test_size']
                                            )

        x_train = x_train.to(args.dev)
        x_test  = x_test.to(args.dev)
        y_train = y_train.to(args.dev)
        y_test  = y_test.to(args.dev)

        train_ds = TensorDataset(x_train, y_train)
        test_ds  = TensorDataset(x_test, y_test)

        train_dl = DataLoader(train_ds, batch_size=cfg['training']['batch_size'], shuffle=True)
        test_dl  = DataLoader(test_ds, batch_size=cfg['training']['batch_size'])

        ### Define model
        px, py = core.setup_probe_coords(
                            N_classes, cfg['geom']['px'], cfg['geom']['py'], cfg['geom']['pd'], 
                            cfg['geom']['Nx'], cfg['geom']['Ny'], cfg['geom']['pml']['N']
                            )
        src_x, src_y = core.setup_src_coords(
                            cfg['geom']['src_x'], cfg['geom']['src_y'], cfg['geom']['Nx'],
                            cfg['geom']['Ny'], cfg['geom']['pml']['N']
                            )

        if cfg['geom']['use_design_region']: # Limit the design region
            design_region = torch.zeros(cfg['geom']['Nx'], cfg['geom']['Ny'], dtype=torch.uint8)
            design_region[src_x+5:np.min(px)-5] = 1 # For now, just hardcode this in
        else: # Let the design region be the enire non-PML area
            design_region = None

        model = core.WaveCell(
                    cfg['geom']['dt'], cfg['geom']['Nx'], cfg['geom']['Ny'], src_x, src_y, px, py,
                    pml_N=cfg['geom']['pml']['N'], pml_p=cfg['geom']['pml']['p'], pml_max=cfg['geom']['pml']['max'], 
                    c0=cfg['geom']['c0'], c1=cfg['geom']['c1'], eta=cfg['geom']['binarization']['eta'], beta=cfg['geom']['binarization']['beta'], 
                    init_rand=cfg['geom']['use_rand_init'], design_region=design_region,
                    nl_b0=cfg['geom']['nonlinearity']['b0'], nl_uth=cfg['geom']['nonlinearity']['uth']
                    )
        model.to(args.dev)

        ### Train
        optimizer = torch.optim.Adam(model.parameters(), lr=cfg['training']['lr'])
        criterion = torch.nn.CrossEntropyLoss()

        # model.train()
        history   = core.train(model, optimizer, criterion, train_dl, test_dl, cfg['training']['N_epochs'], cfg['training']['batch_size'])
        
        ### Print confusion matrix
        cm_test  = core.calc_cm(model, test_dl)
        cm_train = core.calc_cm(model, train_dl)

        ### Save model and results
        if args.name is None:
            args.name = time.strftime("%Y_%m_%d-%H_%M_%S")
        if cfg['training']['prefix'] is not None:
            args.name = cfg['training']['prefix'] + '_' + args.name

        core.save_model(model, args.name, args.savedir, history, cfg, cm_train, cm_test)

    def cross(self, args):
        print("Using configuration from %s: " % args.config)
        with open(args.config, 'r') as ymlfile:
             cfg = yaml.load(ymlfile)
             print(yaml.dump(cfg, default_flow_style=False))

        N_classes = len(cfg['data']['vowels'])

        X, Y = data.load_all_vowels(
                    cfg['data']['vowels'],
                    gender=cfg['data']['gender'], 
                    sr=cfg['data']['sr'], 
                    normalize=True
                    )

        skf = StratifiedKFold(n_splits=args.n_splits, random_state=None, shuffle=True)
        samps = [y.argmax().item() for y in Y]
        num = 1
        for train_index, test_index in skf.split(np.zeros(len(samps)), samps):
            print("Cross validation %d" % num)

            x_train = torch.nn.utils.rnn.pad_sequence([X[i] for i in train_index], batch_first=True)
            x_test = torch.nn.utils.rnn.pad_sequence([X[i] for i in test_index], batch_first=True)
            y_train = torch.nn.utils.rnn.pad_sequence([Y[i] for i in train_index], batch_first=True)
            y_test = torch.nn.utils.rnn.pad_sequence([Y[i] for i in test_index], batch_first=True)

            x_train = x_train.to(args.dev)
            x_test  = x_test.to(args.dev)
            y_train = y_train.to(args.dev)
            y_test  = y_test.to(args.dev)

            train_ds = TensorDataset(x_train, y_train)
            test_ds  = TensorDataset(x_test, y_test)

            train_dl = DataLoader(train_ds, batch_size=cfg['training']['batch_size'], shuffle=True)
            test_dl  = DataLoader(test_ds, batch_size=cfg['training']['batch_size'])

            ### Define model
            px, py = core.setup_probe_coords(
                                N_classes, cfg['geom']['px'], cfg['geom']['py'], cfg['geom']['pd'], 
                                cfg['geom']['Nx'], cfg['geom']['Ny'], cfg['geom']['pml']['N']
                                )
            src_x, src_y = core.setup_src_coords(
                                cfg['geom']['src_x'], cfg['geom']['src_y'], cfg['geom']['Nx'],
                                cfg['geom']['Ny'], cfg['geom']['pml']['N']
                                )

            if cfg['geom']['use_design_region']: # Limit the design region
                design_region = torch.zeros(cfg['geom']['Nx'], cfg['geom']['Ny'], dtype=torch.uint8)
                design_region[src_x+5:np.min(px)-5] = 1 # For now, just hardcode this in
            else: # Let the design region be the enire non-PML area
                design_region = None

            model = core.WaveCell(
                        cfg['geom']['dt'], cfg['geom']['Nx'], cfg['geom']['Ny'], src_x, src_y, px, py,
                        pml_N=cfg['geom']['pml']['N'], pml_p=cfg['geom']['pml']['p'], pml_max=cfg['geom']['pml']['max'], 
                        c0=cfg['geom']['c0'], c1=cfg['geom']['c1'], eta=cfg['geom']['binarization']['eta'], beta=cfg['geom']['binarization']['beta'], 
                        init_rand=cfg['geom']['use_rand_init'], design_region=design_region,
                        nl_b0=cfg['geom']['nonlinearity']['b0'], nl_uth=cfg['geom']['nonlinearity']['uth']
                        )
            model.to(args.dev)

            ### Train
            optimizer = torch.optim.Adam(model.parameters(), lr=cfg['training']['lr'])
            criterion = torch.nn.CrossEntropyLoss()
            history   = core.train(model, optimizer, criterion, train_dl, test_dl, cfg['training']['N_epochs'], cfg['training']['batch_size'])
            
            ### Print confusion matrix
            cm_test  = core.calc_cm(model, test_dl)
            cm_train = core.calc_cm(model, train_dl)

            ### Save model and results
            if args.name is None:
                args.name = time.strftime("%Y_%m_%d-%H_%M_%S")
            if cfg['training']['prefix'] is not None:
                args.name = cfg['training']['prefix'] + '_' + args.name

            args.name += "_cv_" + str(num)

            core.save_model(model, args.name, args.savedir, history, cfg, cm_train, cm_test)

            num += 1

    def summary(self, args):
        model, history, cfg, cm_train, cm_test = core.load_model(args.model_file)

        print("Configuration for model in %s is:" % args.model_file)
        print(yaml.dump(cfg, default_flow_style=False))

        sr = cfg['data']['sr']
        gender = cfg['data']['gender']
        vowels = cfg['data']['vowels']
        train_size = cfg['training']['train_size']
        test_size = cfg['training']['test_size']
        N_classes = len(vowels)

        fig = plt.figure(constrained_layout=True, figsize=(7, 3.5))
        gs = mpl.gridspec.GridSpec(2, 3 , figure=fig, width_ratios=[1, 1, 0.5])
        ax1 = fig.add_subplot(gs[0,0])
        ax2 = fig.add_subplot(gs[1,0], sharex=ax1)
        ax3 = fig.add_subplot(gs[:,1])
        ax4 = fig.add_subplot(gs[0,2])
        ax5 = fig.add_subplot(gs[1,2])

        epochs = range(0,len(history["acc_test"]))
        ax1.plot(epochs, history["loss_train"], "-", label="Training dataset")
        ax1.plot(epochs, history["loss_test"], "-", label="Testing dataset")
        ax1.set_ylabel("Loss")
        ax2.plot(epochs, history["acc_train"], "-", label="Training dataset")
        ax2.plot(epochs, history["acc_test"], "-", label="Testing dataset")
        ax2.set_xlabel("Number of training epochs")
        ax2.set_ylabel("Accuracy")
        ax2.set_ylim(top=1.01)
        ax1.legend()

        viz.plot_c(model, ax=ax3)

        viz.plot_confusion_matrix(cm_train, title="Training dataset", normalize=False, ax=ax4, labels=vowels)
        viz.plot_confusion_matrix(cm_test, title="Testing dataset", normalize=False, ax=ax5, labels=vowels)

        plt.show()

    def inference(self, args):
        if args.name is None:
            raise ValueError("--name must be specified to load a model")

        model, history, cfg, cm_train, cm_test = core.load_model(args.name)

        print("Configuration for model in %s is:" % args.name)
        print(yaml.dump(cfg, default_flow_style=False))

        sr = cfg['data']['sr']
        gender = cfg['data']['gender']
        vowels = cfg['data']['vowels']
        train_size = cfg['training']['train_size']
        test_size = cfg['training']['test_size']
        N_classes = len(vowels)

        if args.fields:
                x_train, x_test, y_train, y_test = data.load_selected_vowels(
                                                vowels,
                                                gender=gender, 
                                                sr=sr, 
                                                normalize=True, 
                                                train_size=N_classes, 
                                                test_size=N_classes
                                            )
                test_ds = TensorDataset(x_test, y_test)  
                fig, axs = plt.subplots(N_classes, 1, constrained_layout=True, figsize=(3.5,6))

                for xb, yb in DataLoader(test_ds, batch_size=1):
                    with torch.no_grad():
                        field_dist = model(xb, probe_output=False)
                        probe_series = field_dist[0, :, model.px, model.py]
                        viz.plot_total_field(model, field_dist, yb, ax=axs[yb.argmax().item()])
                plt.show()

        if args.stft:
                x_train, x_test, y_train, y_test = data.load_selected_vowels(
                                                        vowels,
                                                        gender=gender, 
                                                        sr=sr, 
                                                        normalize=True, 
                                                        train_size=N_classes, 
                                                        test_size=N_classes
                                                    )
                test_ds = TensorDataset(x_test, y_test)  
                fig, axs = plt.subplots(N_classes, N_classes, constrained_layout=True, figsize=(5.5,5.5), sharex=True, sharey=True)

                for xb, yb in DataLoader(test_ds, batch_size=1):
                    with torch.no_grad():
                        field_dist = model(xb, probe_output=False)
                        probe_series = field_dist[0, :, model.px, model.py]
                        for j in range(0, probe_series.shape[1]):
                            i = yb.argmax().item()
                            ax = axs[i, j]
                            input_stft = np.abs(librosa.stft(xb.numpy().squeeze(), n_fft=256))
                            output_stft = np.abs(librosa.stft(probe_series[:,j].numpy(), n_fft=256))

                            librosa.display.specshow(
                                librosa.amplitude_to_db(output_stft,ref=np.max(input_stft)),
                                sr=sr,
                                vmax=0,
                                ax=ax,
                                y_axis='linear',
                                x_axis='time',
                                cmap=plt.cm.inferno
                            )
                            ax.set_ylim([0,sr/4])
                            
                            if j > 0:
                                ax.set_ylabel('')
                            if i < N_classes-1:
                                ax.set_xlabel('')
                            ax.text(0.5, 0.95, '%s at probe #%d' % (vowels[i], j+1), color="w", transform=ax.transAxes, ha="center", va="top", fontsize="large")
                plt.show()

        if args.animate:
            x_train, x_test, y_train, y_test = data.load_selected_vowels(
                                vowels,
                                gender=gender, 
                                sr=sr, 
                                normalize=True, 
                                train_size=N_classes, 
                                test_size=N_classes
                            )

            test_ds = TensorDataset(x_test, y_test)  
            for xb, yb in DataLoader(test_ds, batch_size=1):
                with torch.no_grad():
                    field_dist = model(xb, probe_output=False)
                    viz.animate_fields(model, field_dist, yb)

if __name__ == '__main__':
    WaveTorch()

