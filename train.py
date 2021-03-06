import argparse
import os
from dataloaders import make_data_loader
from models import model
from models.metric import ArcFace
from models.loss import FocalLoss
import torch.optim as optim
import numpy as np
import time
import torch
from tensorboardX import SummaryWriter
from config import configurations
from models.model_irse import IR_SE_50
from utils import *

os.environ['CUDA_VISIBLE_DEVICES'] = '0, 1'

if __name__ == "__main__":
    
    # ======= hyperparameters & data loaders =======#
    cfg = configurations[1]

    SEED = cfg['SEED']  # random seed for reproduce results
    torch.manual_seed(SEED)

    DATA_ROOT = cfg['DATA_ROOT']  # the parent root where your train/val/test data are stored
    MODEL_ROOT = cfg['MODEL_ROOT']  # the root to buffer your checkpoints
    LOG_ROOT = cfg['LOG_ROOT']  # the root to log your train/val status


    BACKBONE_NAME = cfg['BACKBONE_NAME'] 
    HEAD_NAME = cfg['HEAD_NAME']  # support:  ['Softmax', 'ArcFace', 'CosFace', 'SphereFace', 'Am_softmax']
    LOSS_NAME = cfg['LOSS_NAME']  # support: ['Focal', 'Softmax']

    BACKBONE_RESUME_ROOT = cfg['BACKBONE_RESUME_ROOT']
    HEAD_RESUME_ROOT = cfg['HEAD_RESUME_ROOT']


    INPUT_SIZE = cfg['INPUT_SIZE']
    RGB_MEAN = cfg['RGB_MEAN']  # for normalize inputs
    RGB_STD = cfg['RGB_STD']
    EMBEDDING_SIZE = cfg['EMBEDDING_SIZE']  # feature dimension
    BATCH_SIZE = cfg['BATCH_SIZE']
    DROP_LAST = cfg['DROP_LAST']  # whether drop the last batch to ensure consistent batch_norm statistics
    LR = cfg['LR']  # initial LR
    NUM_EPOCH = cfg['NUM_EPOCH']
    WEIGHT_DECAY = cfg['WEIGHT_DECAY']
    MOMENTUM = cfg['MOMENTUM']
    STAGES = cfg['STAGES']  # epoch stages to decay learning rate

    DEVICE = cfg['DEVICE']
    MULTI_GPU = cfg['MULTI_GPU']  # flag to use multiple GPUs
    GPU_ID = cfg['GPU_ID']  # specify your GPU ids
    PIN_MEMORY = cfg['PIN_MEMORY']
    NUM_WORKERS = cfg['NUM_WORKERS']
    print("=" * 60)
    print("Overall Configurations:")
    print(cfg)
    print("=" * 60)

    writer = SummaryWriter(LOG_ROOT)

    # ========== make data loader =============#
    train_loader, NUM_CLASS = make_data_loader(cfg)
    EVAL_ROOT = cfg['EVAL_ROOT']
    lfw, lfw_issame = get_val_data(EVAL_ROOT)

    # ======= model & loss & optimizer =======#
    BACKBONE_DICT = {'IR_SE_50': IR_SE_50(INPUT_SIZE)}

    BACKBONE = BACKBONE_DICT[BACKBONE_NAME]
    print("=" * 60)
    print(BACKBONE)
    print("{} Backbone Generated".format(BACKBONE_NAME))
    print("=" * 60)

    HEAD_DICT = {'ArcFace': ArcFace(in_features=EMBEDDING_SIZE, out_features=NUM_CLASS, device_id=GPU_ID),
                #  'CosFace': CosFace(in_features=EMBEDDING_SIZE, out_features=NUM_CLASS, device_id=GPU_ID),
                #  'SphereFace': SphereFace(in_features=EMBEDDING_SIZE, out_features=NUM_CLASS, device_id=GPU_ID),
                #  'Am_softmax': Am_softmax(in_features=EMBEDDING_SIZE, out_features=NUM_CLASS, device_id=GPU_ID)
                 }

    HEAD = HEAD_DICT[HEAD_NAME]
    print("=" * 60)
    print(HEAD)
    print("{} Head Generated".format(HEAD_NAME))
    print("=" * 60)


    LOSS_DICT = {'Focal': FocalLoss(),
                 'Softmax': torch.nn.CrossEntropyLoss()}
    LOSS = LOSS_DICT[LOSS_NAME]
    print("=" * 60)
    print(LOSS)
    print("{} Loss Generated".format(LOSS_NAME))
    print("=" * 60)

    if BACKBONE_NAME.find("IR") >= 0:
        # separate batch_norm parameters from others; 
        # do not do weight decay for batch_norm parameters to improve the generalizability
        backbone_paras_only_bn, backbone_paras_wo_bn = separate_irse_bn_paras(BACKBONE)  
        _, head_paras_wo_bn = separate_irse_bn_paras(HEAD)
    else:
        backbone_paras_only_bn, backbone_paras_wo_bn = separate_resnet_bn_paras(
            BACKBONE)  # separate batch_norm parameters from others; do not do weight decay for batch_norm parameters to improve the generalizability
        _, head_paras_wo_bn = separate_resnet_bn_paras(HEAD)

    OPTIMIZER = optim.SGD([{'params': backbone_paras_wo_bn + head_paras_wo_bn, 'weight_decay': WEIGHT_DECAY},
                           {'params': backbone_paras_only_bn}], lr=LR, momentum=MOMENTUM)
    print("=" * 60)
    print(OPTIMIZER)
    print("Optimizer Generated")
    print("=" * 60)

    # ======= load saved checkpoints for finetune =======#
    if BACKBONE_RESUME_ROOT and HEAD_RESUME_ROOT:
        print("=" * 60)
        if os.path.isfile(BACKBONE_RESUME_ROOT) and os.path.isfile(HEAD_RESUME_ROOT):
            print("Loading Backbone Checkpoint '{}'".format(BACKBONE_RESUME_ROOT))
            BACKBONE.load_state_dict(torch.load(BACKBONE_RESUME_ROOT))
            print("Loading Head Checkpoint '{}'".format(HEAD_RESUME_ROOT))
            HEAD.load_state_dict(torch.load(HEAD_RESUME_ROOT))
        else:
            print("No Checkpoint Found at '{}' and '{}'. Please Have a Check or Continue to Train from Scratch".format(
                BACKBONE_RESUME_ROOT, HEAD_RESUME_ROOT))
        print("=" * 60)


    if MULTI_GPU:
        # multi-GPU setting
        BACKBONE = torch.nn.DataParallel(BACKBONE, device_ids=GPU_ID)
        BACKBONE = BACKBONE.to(DEVICE)
    else:
        # single-GPU setting
        BACKBONE = BACKBONE.to(DEVICE)

    # ======= train & validation & save checkpoint =======#
    DISP_FREQ = len(train_loader) // 100  # frequency to display training loss & acc

    # NUM_EPOCH_WARM_UP = NUM_EPOCH // 25  # use the first 1/25 epochs to warm up
    # NUM_BATCH_WARM_UP = len(train_loader) * NUM_EPOCH_WARM_UP  # use the first 1/25 epochs to warm up
    batch = 0  # batch index

    for epoch in range(NUM_EPOCH):  # start training process

        # validation statistics per epoch (buffer for visualization)
        print("=" * 60)
        print("Perform Evaluation")

        # acc, best_threshold, roc_curve = perform_val(MULTI_GPU, DEVICE, EMBEDDING_SIZE, BATCH_SIZE, BACKBONE, lfw, lfw_issame)
        # writer.add_scalar('lfw_Accuracy', acc, epoch)
        # writer.add_scalar('lfw_Best_Threshold', best_threshold, epoch)
        # writer.add_image('lfw_ROC_Curve', roc_curve, epoch)

        if epoch == STAGES[0]:
            # adjust LR for each training stage after warm up, you can also choose to adjust LR manually (with slight modification) once plaueau observed
            schedule_lr(OPTIMIZER)
        if epoch == STAGES[1]:
            schedule_lr(OPTIMIZER)
        if epoch == STAGES[2]:
            schedule_lr(OPTIMIZER)

        BACKBONE.train()  # set to training mode
        HEAD.train()

        losses = AverageMeter()
        top1 = AverageMeter()
        # top5 = AverageMeter()

        for inputs, labels in train_loader:

            # if (epoch + 1 <= NUM_EPOCH_WARM_UP) and (batch + 1 <= NUM_BATCH_WARM_UP):  # adjust LR for each training batch during warm up
            #     warm_up_lr(batch + 1, NUM_BATCH_WARM_UP, LR, OPTIMIZER)

            # compute output
            inputs = inputs.to(DEVICE)
            labels = labels.to(DEVICE).long()
            features = BACKBONE(inputs)
            outputs = HEAD(features, labels)
            loss = LOSS(outputs, labels)

            # measure accuracy and record loss
            prec1 = accuracy(outputs.data, labels, topk=(1, ))
            losses.update(loss.data.item(), inputs.size(0))
            top1.update(prec1.data.item(), inputs.size(0))
            # top5.update(prec5.data.item(), inputs.size(0))

            # compute gradient and do SGD step
            OPTIMIZER.zero_grad()
            loss.backward()
            OPTIMIZER.step()

            # dispaly training loss & acc every DISP_FREQ
            if ((batch + 1) % DISP_FREQ == 0) and batch != 0:
                print("=" * 60)
                print('Epoch {}/{} Batch {}/{}\t'
                      'Training Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Training Prec@1 {top1.val:.3f} ({top1.avg:.3f})\t'.format(
                          epoch + 1, NUM_EPOCH, batch + 1, len(train_loader) * NUM_EPOCH, loss=losses, top1=top1))
                print("=" * 60)

            batch += 1  # batch index

        # training statistics per epoch (buffer for visualization)
        epoch_loss = losses.avg
        epoch_acc = top1.avg
        writer.add_scalar("Training_Loss", epoch_loss, epoch + 1)
        writer.add_scalar("Training_Accuracy", epoch_acc, epoch + 1)

        # save checkpoints per epoch
        if MULTI_GPU:
            torch.save(BACKBONE.module.state_dict(), os.path.join(MODEL_ROOT,
                                                                  "Backbone_{}_Epoch_{}_Batch_{}.pth".format(
                                                                      BACKBONE_NAME, epoch + 1, batch)))
            torch.save(HEAD.state_dict(), os.path.join(MODEL_ROOT,
                                                       "Head_{}_Epoch_{}_Batch_{}.pth".format(
                                                           HEAD_NAME, epoch + 1, batch)))
        else:
            torch.save(BACKBONE.state_dict(), os.path.join(MODEL_ROOT,
                                                           "Backbone_{}_Epoch_{}_Batch_{}.pth".format(
                                                               BACKBONE_NAME, epoch + 1, batch)))
            torch.save(HEAD.state_dict(), os.path.join(MODEL_ROOT,
                                                       "Head_{}_Epoch_{}_Batch_{}.pth".format(
                                                           HEAD_NAME, epoch + 1, batch)))
