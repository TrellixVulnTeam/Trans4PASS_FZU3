import glob
import shutil
import os, sys
os.chdir(sys.path[0])
import torch
import torch.nn as nn
from torch.utils import data
import numpy as np
from torch.autograd import Variable

import torch.optim as optim
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import logging, sys
import time
from tensorboardX import SummaryWriter
from model.trans4passplus import Trans4PASS_plus_v1, Trans4PASS_plus_v2
from model.discriminator import FCDiscriminator
from model.memory import init_memory
from dataset.cs13_dataset_src import CS13SrcDataSet
from dataset.dp13_dataset import densepass13DataSet, densepass13TestDataSet
import argparse
import os.path as osp
from compute_iou import fast_hist, per_class_iu
from utils.loss import feat_kl_loss

IMG_MEAN = np.array((104.00698793, 116.66876762, 122.67891434), dtype=np.float32)

MODEL = 'Trans4PASS_plus_v2'
EMB_CHANS = 128
BATCH_SIZE = 1
ITER_SIZE = 1
NUM_WORKERS = BATCH_SIZE * 2
SOURCE_NAME = 'CS13'
TARGET_NAME = 'CS132DP13'
DATA_DIRECTORY = 'datasets/cityscapes'
DATA_LIST_PATH = 'dataset/cityscapes_list/train.txt'
IGNORE_LABEL = 255
INPUT_SIZE = '1024,512'
DATA_DIRECTORY_TARGET = 'datasets/DensePASS'
DATA_LIST_PATH_TARGET = 'dataset/densepass_list/train.txt'
SSL_DIR = './pseudo_{}_{}_ms'.format(TARGET_NAME, MODEL)
DATA_LIST_PATH_TARGET_TEST = 'dataset/densepass_list/val.txt'
INPUT_SIZE_TARGET = '2048,400'
TARGET_TRANSFORM = 'resize'
INPUT_SIZE_TARGET_TEST = '2048,400'
LEARNING_RATE = 2.5e-6
MOMENTUM = 0.9
NUM_CLASSES = 13
NUM_STEPS = 100000
NUM_STEPS_STOP = 80000 # early stopping
NUM_PROTOTYPE = 50
POWER = 0.9
RANDOM_SEED = 1234
RESTORE_FROM = 'snapshots/CS132DP13_Trans4PASS_plus_v2_WarmUp/BestCS132DP13_G.pth'
SAVE_NUM_IMAGES = 2
SAVE_PRED_EVERY = 100
DIR_NAME = '{}2{}_{}_MPA/'.format(SOURCE_NAME, TARGET_NAME, MODEL)
SNAPSHOT_DIR = './snapshots/' + DIR_NAME
WEIGHT_DECAY = 0.0005
# LOG_DIR = './log'
LOG_DIR = SNAPSHOT_DIR
SAVE_PATH = './result/' + DIR_NAME

LEARNING_RATE_D = 1e-4
LAMBDA_ADV_TARGET = 0.01

# ---- memory
MOMENTUM_MEM = 0.999
ITER_UPDATE_MEM = 100
# --- pseudo label
LAMBDA_SSL = 1
SOURCE = 'cityscapes13'
LAMBDA_KL_S = 0.001
LAMBDA_KL_T = 0.001
TARGET = 'densepass13'
SET = 'train'

NAME_CLASSES = ['road', 'sidewalk', 'building', 'wall', 'fence', 'pole', 'traffic light',
                'traffic sign', 'vegetation', 'terrain', 'sky', 'person', 'car']

def get_arguments():
    """Parse all the arguments provided from the CLI.

    Returns:
      A list of parsed arguments.
    """
    parser = argparse.ArgumentParser(description="Network")
    parser.add_argument("--model", type=str, default=MODEL,
                        help="available options : Trans4PASS_plus_v1, Trans4PASS_plus_v2")
    parser.add_argument("--emb-chans", type=int, default=EMB_CHANS,
                        help="Number of channels in decoder head.")
    parser.add_argument("--source", type=str, default=SOURCE,
                        help="available options : cityscapes, synpass, structured3d, stanford2d3dpin")
    parser.add_argument("--target", type=str, default=TARGET,
                        help="available options : densepass, stanford2d3dpan")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help="Number of images sent to the network in one step.")
    parser.add_argument("--iter-size", type=int, default=ITER_SIZE,
                        help="Accumulate gradients for ITER_SIZE iterations.")
    parser.add_argument("--num-workers", type=int, default=NUM_WORKERS,
                        help="number of workers for multithread dataloading.")
    parser.add_argument("--data-dir", type=str, default=DATA_DIRECTORY,
                        help="Path to the directory containing the source dataset.")
    parser.add_argument("--data-list", type=str, default=DATA_LIST_PATH,
                        help="Path to the file listing the images in the source dataset.")
    parser.add_argument("--ignore-label", type=int, default=IGNORE_LABEL,
                        help="The index of the label to ignore during the training.")
    parser.add_argument("--input-size", type=str, default=INPUT_SIZE,
                        help="Comma-separated string with height and width of source images.")
    parser.add_argument("--data-dir-target", type=str, default=DATA_DIRECTORY_TARGET,
                        help="Path to the directory containing the target dataset.")
    parser.add_argument("--data-list-target", type=str, default=DATA_LIST_PATH_TARGET,
                        help="Path to the file listing the images in the target dataset.")
    parser.add_argument("--data-list-target-test", type=str, default=DATA_LIST_PATH_TARGET_TEST,
                        help="Path to the file listing the images in the target val dataset.")
    parser.add_argument("--input-size-target", type=str, default=INPUT_SIZE_TARGET,
                        help="Comma-separated string with height and width of target images.")
    parser.add_argument("--input-size-target-test", type=str, default=INPUT_SIZE_TARGET_TEST,
                        help="Comma-separated string with height and width of target images.")
    parser.add_argument("--is-training", action="store_true",
                        help="Whether to updates the running means and variances during the training.")
    parser.add_argument("--learning-rate", type=float, default=LEARNING_RATE,
                        help="Base learning rate for training with polynomial decay.")
    parser.add_argument("--learning-rate-D", type=float, default=LEARNING_RATE_D,
                        help="Base learning rate for discriminator.")
    parser.add_argument("--lambda-adv-target", type=float, default=LAMBDA_ADV_TARGET,
                        help="lambda_adv for adversarial training.")
    parser.add_argument("--momentum", type=float, default=MOMENTUM,
                        help="Momentum component of the optimiser.")
    parser.add_argument("--not-restore-last", action="store_true",
                        help="Whether to not restore last (FC) layers.")
    parser.add_argument("--num-classes", type=int, default=NUM_CLASSES,
                        help="Number of classes to predict (including background).")
    parser.add_argument("--num-steps", type=int, default=NUM_STEPS,
                        help="Number of training steps.")
    parser.add_argument("--num-steps-stop", type=int, default=NUM_STEPS_STOP,
                        help="Number of training steps for early stopping.")
    parser.add_argument("--num-prototype", type=int, default=NUM_PROTOTYPE,
                        help="Number of prototypes.")
    parser.add_argument("--power", type=float, default=POWER,
                        help="Decay parameter to compute the learning rate.")
    parser.add_argument("--random-mirror", action="store_true",
                        help="Whether to randomly mirror the inputs during the training.")
    parser.add_argument("--random-scale", action="store_true",
                        help="Whether to randomly scale the inputs during the training.")
    parser.add_argument("--random-seed", type=int, default=RANDOM_SEED,
                        help="Random seed to have reproducible results.")
    parser.add_argument("--restore-from", type=str, default=RESTORE_FROM,
                        help="Where restore model parameters from.")
    parser.add_argument("--save-num-images", type=int, default=SAVE_NUM_IMAGES,
                        help="How many images to save.")
    parser.add_argument("--save-pred-every", type=int, default=SAVE_PRED_EVERY,
                        help="Save summaries and checkpoint every often.")
    parser.add_argument("--snapshot-dir", type=str, default=SNAPSHOT_DIR,
                        help="Where to save snapshots of the model.")
    parser.add_argument("--weight-decay", type=float, default=WEIGHT_DECAY,
                        help="Regularisation parameter for L2-loss.")
    parser.add_argument("--cpu", action='store_true', help="choose to use cpu device.")
    parser.add_argument("--tensorboard", action='store_true', help="choose whether to use tensorboard.")
    parser.add_argument("--log-dir", type=str, default=LOG_DIR,
                        help="Path to the directory of log.")
    parser.add_argument("--set", type=str, default=SET,
                        help="choose adaptation set.")
    parser.add_argument("--continue-train", action="store_true",
                        help="continue training")
    parser.add_argument("--save", type=str, default=SAVE_PATH,
                        help="Path to save result.")
    return parser.parse_args()

args = get_arguments()

def setup_logger(name, save_dir, filename="log.txt", mode='w'):
    logging.root.name = name
    logging.root.setLevel(logging.INFO)
    # don't log results for the non-master process
    formatter = logging.Formatter("%(levelname)s: %(message)s")
    if save_dir:
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        fh = logging.FileHandler(os.path.join(save_dir, filename), mode=mode)  # 'a+' for add, 'w' for overwrite
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logging.root.addHandler(fh)
    # else:
    ch = logging.StreamHandler(stream=sys.stdout)
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(formatter)
    logging.root.addHandler(ch)

setup_logger('Trans4PASS', SNAPSHOT_DIR)

def lr_poly(base_lr, iter, max_iter, power):
    return base_lr * ((1 - float(iter) / max_iter) ** (power))


def adjust_learning_rate(optimizer, i_iter):
    lr = lr_poly(args.learning_rate, i_iter, args.num_steps, args.power)
    optimizer.param_groups[0]['lr'] = lr
    if len(optimizer.param_groups) > 1:
        optimizer.param_groups[1]['lr'] = lr * 10


def adjust_learning_rate_D(optimizer, i_iter):
    lr = lr_poly(args.learning_rate_D, i_iter, args.num_steps, args.power)
    optimizer.param_groups[0]['lr'] = lr
    if len(optimizer.param_groups) > 1:
        optimizer.param_groups[1]['lr'] = lr * 10


def amp_backward(loss, optimizer, retain_graph=False):
    loss.backward(retain_graph=retain_graph)


def load_my_state_dict(model, state_dict):  # custom function to load model when not all dict elements
    own_state = model.state_dict()
    for name, param in state_dict.items():
        if name not in own_state:
            if name.startswith("module."):
                own_state[name.split("module.")[-1]].copy_(param)
            else:
                logging.info(name, " not loaded")
                continue
        else:
            own_state[name].copy_(param)
    return model

def main():
    """Create the model and start the training."""

    device = torch.device("cuda" if not args.cpu else "cpu")
    cudnn.benchmark = True
    cudnn.enabled = True

    w, h = map(int, args.input_size.split(','))
    input_size = (w, h)

    w, h = map(int, args.input_size_target.split(','))
    input_size_target = (w, h)

    w, h = map(int, args.input_size_target_test.split(','))
    input_size_target_test = (w, h)

    Iter = 0
    bestIoU = 0
    mIoU = 0

    # Create network
    # init G
    if args.model == 'Trans4PASS_plus_v1':
        model = Trans4PASS_plus_v1(num_classes=args.num_classes, emb_chans=args.emb_chans)
    elif args.model == 'Trans4PASS_plus_v2':
        model = Trans4PASS_plus_v2(num_classes=args.num_classes, emb_chans=args.emb_chans)
    else:
        raise ValueError
    saved_state_dict = torch.load(args.restore_from, map_location=lambda storage, loc: storage)
    if 'state_dict' in saved_state_dict.keys():
        saved_state_dict = saved_state_dict['state_dict']
    if args.continue_train:
        if list(saved_state_dict.keys())[0].split('.')[0] == 'module':
            for key in saved_state_dict.keys():
                saved_state_dict['.'.join(key.split('.')[1:])] = saved_state_dict.pop(key)
        model.load_state_dict(saved_state_dict)
    else:
        # model = load_my_state_dict(model, saved_state_dict)
        msg = model.load_state_dict(saved_state_dict, strict=False)
        logging.info(msg)

    # init D
    model_D = FCDiscriminator(num_classes=args.num_classes).to(device)

    init_mem_j_path = 'init_memory_joint_ms_{}_CS13.npy'.format(MODEL)
    if not os.path.exists(init_mem_j_path):
        trainset_temp = densepass13DataSet(args.data_dir_target, args.data_list_target, crop_size=input_size_target, set='train',
                                         ssl_dir=SSL_DIR)
        trainloader_temp = data.DataLoader(trainset_temp, batch_size=1, shuffle=False)
        testset_temp = CS13SrcDataSet(args.data_dir, args.data_list, crop_size=input_size, set='train')
        testloader_temp = data.DataLoader(testset_temp, batch_size=1, shuffle=False)
        init_mem = init_memory(trainloader_temp, testloader_temp, model, num_classes=args.num_classes, save_path=init_mem_j_path)
        del trainloader_temp, trainset_temp, testset_temp, testloader_temp
    else:
        init_mem = np.load(init_mem_j_path)
    init_mem = torch.from_numpy(init_mem).to(device).to(torch.get_default_dtype())
    init_batch_mem = [[] for _ in range(NUM_CLASSES)]

    model.train()
    model.to(device)

    model_D.train()
    model_D.to(device)

    if not os.path.exists(args.snapshot_dir):
        os.makedirs(args.snapshot_dir)
    else:
        script = os.path.abspath(__file__)
        shutil.copy(script, args.snapshot_dir)

    # init data loader
    trainset = CS13SrcDataSet(args.data_dir, args.data_list, max_iters=args.num_steps * args.iter_size * args.batch_size,
                            crop_size=input_size, scale=args.random_scale, mirror=args.random_mirror, mean=IMG_MEAN, set=args.set)
    trainloader = data.DataLoader(trainset, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers,
                                  pin_memory=True)
    trainloader_iter = enumerate(trainloader)
    # --- SSL_DIR
    targetset = densepass13DataSet(args.data_dir_target, args.data_list_target, max_iters=args.num_steps * args.iter_size * args.batch_size,
                                 crop_size=input_size_target, scale=False, mirror=args.random_mirror, mean=IMG_MEAN, set=args.set,
                                 ssl_dir=SSL_DIR, trans=TARGET_TRANSFORM)
    targetloader = data.DataLoader(targetset, batch_size=args.batch_size, shuffle=True, num_workers=args.num_workers,
                                   pin_memory=True)
    targetloader_iter = enumerate(targetloader)

    logging.info('\n--- load TEST dataset ---')

    # test_h, test_w = 400, 2048
    test_w, test_h = input_size_target_test
    targettestset = densepass13TestDataSet(args.data_dir_target, args.data_list_target_test, crop_size=(test_w, test_h),
                                         mean=IMG_MEAN, scale=False, mirror=False, set='val')
    testloader = data.DataLoader(targettestset, batch_size=1, shuffle=False, num_workers=1, pin_memory=False)
    # test_interp = nn.Upsample(size=(test_h, test_w), mode='bilinear', align_corners=True)

    model.train()
    # init optimizer
    optimizer = optim.SGD(model.optim_parameters(args), 
                        lr=args.learning_rate, momentum=args.momentum, weight_decay=args.weight_decay)
    optimizer.zero_grad()

    optimizer_D = optim.Adam(model_D.parameters(), lr=args.learning_rate_D, betas=(0.9, 0.99))
    optimizer_D.zero_grad()

    # init loss
    bce_loss = torch.nn.BCEWithLogitsLoss()
    seg_loss = torch.nn.CrossEntropyLoss(ignore_index=255)
    seg_loss_target = torch.nn.CrossEntropyLoss(ignore_index=255)

    interp = nn.Upsample(size=(input_size[1], input_size[0]), mode='bilinear', align_corners=True)
    interp_target = nn.Upsample(size=(input_size_target[1], input_size_target[0]), mode='bilinear', align_corners=True)

    source_label = 0
    target_label = 1

    if args.tensorboard:
        if not os.path.exists(args.log_dir):
            os.makedirs(args.log_dir)
        writer = SummaryWriter(args.log_dir)


    # start training
    for i_iter in range(Iter, args.num_steps):

        loss_seg_value = 0
        loss_seg_value_t = 0
        loss_adv_target_value = 0
        loss_D_value = 0
        loss_kl_s_value = 0
        loss_kl_t_value = 0

        optimizer.zero_grad()
        adjust_learning_rate(optimizer, i_iter)

        optimizer_D.zero_grad()
        adjust_learning_rate_D(optimizer_D, i_iter)

        for sub_i in range(args.iter_size):
            # train G
            for param in model_D.parameters():
                param.requires_grad = False

            # train with source
            _, batch = trainloader_iter.__next__()
            images, labels, _, _ = batch
            images = images.to(device)
            labels = labels.long().to(device)

            src_features, pred = model(images)
            src_feature = sum(src_features)
            loss_kl_feat_src, batch_feats_mem_s, select_feat_src = feat_kl_loss(src_feature, labels, init_mem)
            pred = interp(pred)
            loss_seg = seg_loss(pred, labels)
            loss = loss_seg + loss_kl_feat_src * LAMBDA_KL_S

            # proper normalization
            loss = loss / args.iter_size
            amp_backward(loss, optimizer)
            loss_seg_value += loss_seg.item() / args.iter_size
            loss_kl_s_value += loss_kl_feat_src.item() / args.iter_size

            _, batch = targetloader_iter.__next__()
            images, trg_labels, _, _ = batch
            images = images.to(device)
            trg_labels = trg_labels.long().to(device)

            trg_features, pred_target = model(images)
            trg_feature = sum(trg_features)
            loss_kl_feat_trg, batch_feats_mem_t, select_feat_trg = feat_kl_loss(trg_feature, trg_labels, init_mem)
            pred_target = interp_target(pred_target)
            loss_seg_trg = seg_loss_target(pred_target, trg_labels)
            D_out = model_D(F.softmax(pred_target, dim=1))
            loss_adv_target = bce_loss(D_out, torch.FloatTensor(D_out.data.size()).fill_(source_label).to(device))
            loss = loss_seg_trg * LAMBDA_SSL + loss_kl_feat_trg * LAMBDA_KL_T + args.lambda_adv_target * loss_adv_target
            loss = loss / args.iter_size
            amp_backward(loss, optimizer)
            loss_seg_value_t += loss_seg_trg.item() / args.iter_size
            loss_adv_target_value += loss_adv_target.item() / args.iter_size
            loss_kl_t_value += loss_kl_feat_trg.item() / args.iter_size

            # === train D
            for param in model_D.parameters():
                param.requires_grad = True

            # train with source
            pred = pred.detach()
            D_out = model_D(F.softmax(pred, dim=1))

            loss_D = bce_loss(D_out, torch.FloatTensor(D_out.data.size()).fill_(source_label).to(device))
            loss_D = loss_D / args.iter_size / 2
            amp_backward(loss_D, optimizer_D)
            loss_D_value += loss_D.item()

            # train with target
            pred_target = pred_target.detach()
            D_out = model_D(F.softmax(pred_target, dim=1))

            loss_D = bce_loss(D_out, torch.FloatTensor(D_out.data.size()).fill_(target_label).to(device))
            loss_D = loss_D / args.iter_size / 2
            amp_backward(loss_D, optimizer_D)
            loss_D_value += loss_D.item()

            for clsid in range(NUM_CLASSES):
                feat_cls_s = batch_feats_mem_s[clsid].cpu().data.numpy()
                feat_cls_t = batch_feats_mem_t[clsid].cpu().data.numpy()
                if feat_cls_s.mean() != 0:
                    init_batch_mem[clsid].append(batch_feats_mem_s[clsid].cpu().data.numpy())
                if feat_cls_t.mean() != 0:
                    init_batch_mem[clsid].append(batch_feats_mem_t[clsid].cpu().data.numpy())
            if i_iter % ITER_UPDATE_MEM == 0 and i_iter > 0:
                cluster_batch_feats_mem_s = torch.zeros_like(init_mem)
                for clsid in range(NUM_CLASSES):
                    if len(init_batch_mem[clsid]) > 1:
                        batch_center = np.mean(init_batch_mem[clsid])[None, ...]
                        cluster_batch_feats_mem_s[clsid] = torch.from_numpy(batch_center).to(cluster_batch_feats_mem_s.dtype)
                # ema
                init_mem = init_mem * MOMENTUM_MEM + cluster_batch_feats_mem_s * (1-MOMENTUM_MEM)

        optimizer.step()
        optimizer_D.step()

        if args.tensorboard:
            scalar_info = {
                'loss_seg': loss_seg_value,
                'loss_seg_t': loss_seg_value_t,
                "loss_kl_s_value": loss_kl_s_value,
                "loss_kl_t_value": loss_kl_t_value,
                'loss_adv_target': loss_adv_target_value,
                'loss_D': loss_D_value
            }

            if i_iter % 10 == 0:
                for key, val in scalar_info.items():
                    writer.add_scalar(key, val, i_iter)
        if i_iter % 10 == 0:
            logging.info('iter={0:8d}/{1:8d}, l_seg={2:.3f}, l_seg_t={7:.3f}, l_adv={3:.3f} l_D={4:.3f}, l_kl_s={5:.3f}, l_kl_t={6:.3f}'.format(
                  i_iter, args.num_steps, loss_seg_value, loss_adv_target_value, loss_D_value, loss_kl_s_value, loss_kl_t_value, loss_seg_value_t))

        if i_iter >= args.num_steps_stop - 1:
            logging.info('save model ...')
            torch.save(model.state_dict(), osp.join(args.snapshot_dir, 'CS_' + str(args.num_steps_stop) + '.pth'))
            torch.save(model_D.state_dict(), osp.join(args.snapshot_dir, 'CS_' + str(args.num_steps_stop) + '_D.pth'))
            break

        if i_iter % args.save_pred_every == 0 and i_iter != 0:
            logging.info('taking snapshot ...')
            # if not os.path.exists(args.save):
            #     os.makedirs(args.save)
            model.eval()
            hist = np.zeros((args.num_classes, args.num_classes))
            for index, batch in enumerate(testloader):
                image, label, _, name = batch
                with torch.no_grad():
                    output1, output2 = model(Variable(image).to(device))
                output = output2.cpu().data[0].numpy()
                output = output.transpose(1,2,0)
                output = np.asarray(np.argmax(output, axis=2), dtype=np.uint8)
                label = label.cpu().data[0].numpy()
                hist += fast_hist(label.flatten(), output.flatten(), args.num_classes)
            mIoUs = per_class_iu(hist)
            for ind_class in range(args.num_classes):
                logging.info('===>{:<15}:\t{}'.format(NAME_CLASSES[ind_class], str(round(mIoUs[ind_class] * 100, 2))))
            mIoU = round(np.nanmean(mIoUs) * 100, 2)
            logging.info('===> mIoU: ' + str(mIoU))
            if mIoU > bestIoU:
                bestIoU = mIoU
                pre_filename = osp.join(args.snapshot_dir, 'Best*.pth')
                pre_filename = glob.glob(pre_filename)
                try:
                    for p in pre_filename:
                        os.remove(p)
                except OSError as e:
                    logging.info(e)
                torch.save(model.state_dict(), osp.join(args.snapshot_dir, 'Best{}2{}_{}iter_{}miou.pth'.format(
                    SOURCE_NAME, TARGET_NAME, str(i_iter), str(bestIoU))))
                torch.save(model_D.state_dict(), osp.join(args.snapshot_dir, 'Best{}2{}_{}iter_D_{}miou.pth'.format(
                    SOURCE_NAME, TARGET_NAME, str(i_iter), str(bestIoU))))
            model.train()

    if args.tensorboard:
        writer.close()


if __name__ == '__main__':
    main()
