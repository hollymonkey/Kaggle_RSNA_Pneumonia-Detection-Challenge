# -*- coding: utf-8 -*-
# File: model.py

import tensorflow as tf

from tensorpack.tfutils.summary import add_moving_summary
from tensorpack.tfutils.argscope import argscope
from tensorpack.tfutils.scope_utils import under_name_scope
from tensorpack.models import (
    Conv2D, FullyConnected, layer_register)
from tensorpack.utils.argtools import memoized

from basemodel import GroupNorm
from utils.box_ops import pairwise_iou
from model_box import encode_bbox_target, decode_bbox_target
from config import config as cfg


@under_name_scope()
def proposal_metrics(iou):
    """
    Add summaries for RPN proposals.

    Args:
        iou: nxm, #proposal x #gt
    """
    # find best roi for each gt, for summary only
    best_iou = tf.reduce_max(iou, axis=0)
    mean_best_iou = tf.reduce_mean(best_iou, name='best_iou_per_gt')
    summaries = [mean_best_iou]
    with tf.device('/cpu:0'):
        for th in [0.3, 0.5]:
            recall = tf.truediv(
                tf.count_nonzero(best_iou >= th),
                tf.size(best_iou, out_type=tf.int64),
                name='recall_iou{}'.format(th))
            summaries.append(recall)
    add_moving_summary(*summaries)


@under_name_scope()
def sample_fast_rcnn_targets(boxes, gt_boxes, gt_labels):
    """
    Sample some ROIs from all proposals for training.
    #fg is guaranteed to be > 0, because grount truth boxes are added as RoIs.

    Args:
        boxes: nx4 region proposals, floatbox
        gt_boxes: mx4, floatbox
        gt_labels: m, int32

    Returns:
        A BoxProposals instance.
        sampled_boxes: tx4 floatbox, the rois
        sampled_labels: t int64 labels, in [0, #class). Positive means foreground.
        fg_inds_wrt_gt: #fg indices, each in range [0, m-1].
            It contains the matching GT of each foreground roi.
    """

    # num_gt_fg = tf.size(tf.where(gt_labels > 0)[:, 0], out_type=tf.int64)
    # is_empty_fg = tf.equal(num_gt_fg, 0)

    iou = pairwise_iou(boxes, gt_boxes)  # nxm
    proposal_metrics(iou)

    # boxes = tf.where(is_empty_fg, boxes, tf.concat([boxes, gt_boxes], axis=0))
    # iou = tf.where(is_empty_fg, iou, tf.concat([iou, tf.eye(tf.shape(gt_boxes)[0])], axis=0))

    # add ground truth as proposals as well
    # boxes = tf.concat([boxes, gt_boxes], axis=0)    # (n+m) x 4
    # iou = tf.concat([iou, tf.eye(tf.shape(gt_boxes)[0])], axis=0)   # (n+m) x m
    # #proposal=n+m from now on
    max_iou = tf.reduce_max(iou, axis=1)
    # fg_mask = tf.reduce_max(iou, axis=1) >= cfg.FRCNN.FG_THRESH

    fg_inds = tf.reshape(tf.where(max_iou >= cfg.FRCNN.FG_THRESH), [-1])
    #fg_inds2 = tf.concat([fg_inds, fg_inds], axis=0)
    #fg_inds = tf.concat([fg_inds2, fg_inds2], axis=0)
    num_fg = tf.minimum(int(cfg.FRCNN.BATCH_PER_IM * cfg.FRCNN.FG_RATIO),
                        tf.size(fg_inds), name='num_fg')
    fg_inds = tf.random_shuffle(fg_inds)[:num_fg]
    bg_cond = tf.logical_and(max_iou < cfg.FRCNN.BG_THRESH, max_iou >= 0)
    bg_inds = tf.reshape(tf.where(bg_cond), [-1])
    num_bg = tf.minimum(
        cfg.FRCNN.BATCH_PER_IM - num_fg,
        tf.size(bg_inds), name='num_bg')
    bg_inds = tf.random_shuffle(bg_inds)[:num_bg]

    add_moving_summary(num_fg, num_bg)

    # fg,bg indices w.r.t proposals

    best_iou_ind = tf.argmax(iou, axis=1)  # #proposal, each in 0~m-1
    fg_inds_wrt_gt = tf.gather(best_iou_ind, fg_inds)  # num_fg

    all_indices = tf.concat([fg_inds, bg_inds], axis=0)  # indices w.r.t all n+m proposal boxes
    ret_boxes = tf.gather(boxes, all_indices)

    ret_labels = tf.concat(
        [tf.gather(gt_labels, fg_inds_wrt_gt),
         tf.zeros_like(bg_inds, dtype=tf.int64)], axis=0)
    # stop the gradient -- they are meant to be training targets
    return BoxProposals(
        tf.stop_gradient(ret_boxes, name='sampled_proposal_boxes'),
        tf.stop_gradient(ret_labels, name='sampled_labels'),
        tf.stop_gradient(fg_inds_wrt_gt),
        gt_boxes, gt_labels)


@layer_register(log_shape=True)
def fastrcnn_outputs(feature, num_classes, class_agnostic_regression=False):
    """
    Args:
        feature (any shape):
        num_classes(int): num_category + 1
        class_agnostic_regression (bool): if True, regression to N x 1 x 4

    Returns:
        cls_logits: N x num_class classification logits
        reg_logits: N x num_classx4 or Nx2x4 if class agnostic
    """
    classification = FullyConnected(
        'class', feature, num_classes,
        kernel_initializer=tf.random_normal_initializer(stddev=0.01))
    num_classes_for_box = 1 if class_agnostic_regression else num_classes
    box_regression = FullyConnected(
        'box', feature, num_classes_for_box * 4,
        kernel_initializer=tf.random_normal_initializer(stddev=0.001))
    box_regression = tf.reshape(box_regression, (-1, num_classes_for_box, 4), name='output_box')
    return classification, box_regression


@layer_register(log_shape=True)
def fastrcnn_outputs_iou(feature, num_classes, class_agnostic_regression=False):
    """
    Args:
        feature (any shape):
        num_classes(int): num_category + 1
        class_agnostic_regression (bool): if True, regression to N x 1 x 4

    Returns:
        cls_logits: N x num_class classification logits
        reg_logits: N x num_classx4 or Nx2x4 if class agnostic
    """
    classification = FullyConnected(
        'class', feature, num_classes,
        kernel_initializer=tf.random_normal_initializer(stddev=0.01))
    num_classes_for_box = 1 if class_agnostic_regression else num_classes
    box_regression = FullyConnected(
        'box', feature, num_classes_for_box * 4,
        kernel_initializer=tf.random_normal_initializer(stddev=0.001))
    box_regression = tf.reshape(box_regression, (-1, num_classes_for_box, 4), name='output_box')

    box_overlap = FullyConnected(
        'iou', feature, num_classes_for_box,
        kernel_initializer=tf.random_normal_initializer(stddev=0.01))
    return classification, box_regression, box_overlap


@under_name_scope()
def fastrcnn_losses(labels, label_logits, fg_boxes, fg_box_logits):
    """
    Args:
        labels: n,
        label_logits: nxC
        fg_boxes: nfgx4, encoded
        fg_box_logits: nfgxCx4 or nfgx1x4 if class agnostic

    Returns:
        label_loss, box_loss
    """
    label_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
        labels=labels, logits=label_logits)
    label_loss = tf.reduce_mean(label_loss * 500., name='label_loss')

    fg_inds = tf.where(labels > 0)[:, 0]
    fg_labels = tf.gather(labels, fg_inds)
    num_fg = tf.size(fg_inds, out_type=tf.int64)
    empty_fg = tf.equal(num_fg, 0)
    if int(fg_box_logits.shape[1]) > 1:
        indices = tf.stack(
            [tf.range(num_fg), fg_labels], axis=1)  # #fgx2
        fg_box_logits = tf.gather_nd(fg_box_logits, indices)
    else:
        fg_box_logits = tf.reshape(fg_box_logits, [-1, 4])

    with tf.name_scope('label_metrics'), tf.device('/cpu:0'):
        prediction = tf.argmax(label_logits, axis=1, name='label_prediction')
        correct = tf.to_float(tf.equal(prediction, labels))  # boolean/integer gather is unavailable on GPU
        accuracy = tf.reduce_mean(correct, name='accuracy')
        fg_label_pred = tf.argmax(tf.gather(label_logits, fg_inds), axis=1)
        num_zero = tf.reduce_sum(tf.to_int64(tf.equal(fg_label_pred, 0)), name='num_zero')
        false_negative = tf.where(
            empty_fg, 0., tf.to_float(tf.truediv(num_zero, num_fg)), name='false_negative')
        fg_accuracy = tf.where(
            empty_fg, 0., tf.reduce_mean(tf.gather(correct, fg_inds)), name='fg_accuracy')

    box_loss = tf.where(empty_fg, 0.,
                        tf.truediv(
                            tf.losses.huber_loss(
                                fg_boxes, fg_box_logits, reduction=tf.losses.Reduction.SUM),
                            tf.to_float(tf.shape(labels)[0])),
                        name='box_loss'
                        )

    # box_loss = tf.losses.huber_loss(
    #     fg_boxes, fg_box_logits, reduction=tf.losses.Reduction.SUM)
    # box_loss = tf.truediv(
    #     box_loss, tf.to_float(tf.shape(labels)[0]), name='box_loss')

    add_moving_summary(label_loss, box_loss, accuracy,
                       fg_accuracy, false_negative, tf.to_float(num_fg, name='num_fg_label'))
    return label_loss, box_loss


@under_name_scope()
def fastrcnn_losses_iou(labels, label_logits, iou_labels, iou_logits, fg_boxes, fg_box_logits):
    """
    Args:
        labels: n,
        label_logits: nxC
        iou_labels: n
        iou_logits: n
        fg_boxes: nfgx4, encoded
        fg_box_logits: nfgxCx4 or nfgx1x4 if class agnostic

    Returns:
        label_loss, box_loss
    """
    ce_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
        labels=labels, logits=label_logits)
    label_loss = tf.reduce_mean(ce_loss * 3., name='label_loss')

#    alpha = 0.75
#    gamma = 2.0
#    softmax_logits = tf.nn.softmax(label_logits)
#    labels_one_hot = tf.one_hot(labels,len(cfg.DATA.CLASS_NAMES))
#    probs_t = tf.reduce_sum(softmax_logits * labels_one_hot, axis=1)
# 
#    probs_t = tf.cast(probs_t,tf.float32)
#    alpha_t = tf.ones(cfg.FRCNN.BATCH_PER_IM) * alpha
#    alpha_t = tf.where(labels > 0, alpha_t, 1.0 - alpha_t)
#    
#    weight_matrix = alpha_t * tf.pow((1.0 - probs_t), gamma)
#    label_loss = weight_matrix * ce_loss
#    
#    #n_pos = tf.reduce_sum(labels)
#    n_false = tf.reduce_sum(tf.cast(tf.greater(ce_loss, -tf.log(0.5)), tf.float32))
#    def has_pos():
#        return tf.reduce_sum(label_loss) / tf.cast(n_false, tf.float32)
#    def no_pos():
#        return tf.reduce_sum(label_loss)
#    label_loss = tf.where(n_false > 0, has_pos, no_pos, name='label_loss')
    

    fg_inds = tf.where(labels > 0)[:, 0]
    fg_labels = tf.gather(labels, fg_inds)
    num_fg = tf.size(fg_inds, out_type=tf.int64)
    empty_fg = tf.equal(num_fg, 0)
    if int(fg_box_logits.shape[1]) > 1:
        indices = tf.stack(
            [tf.range(num_fg), fg_labels], axis=1)  # #fgx2
        fg_box_logits = tf.gather_nd(fg_box_logits, indices)
    else:
        fg_box_logits = tf.reshape(fg_box_logits, [-1, 4])

    with tf.name_scope('label_metrics'), tf.device('/cpu:0'):
        prediction = tf.argmax(label_logits, axis=1, name='label_prediction')
        correct = tf.to_float(tf.equal(prediction, labels))  # boolean/integer gather is unavailable on GPU
        accuracy = tf.reduce_mean(correct, name='accuracy')
        fg_label_pred = tf.argmax(tf.gather(label_logits, fg_inds), axis=1)
        num_zero = tf.reduce_sum(tf.to_int64(tf.equal(fg_label_pred, 0)), name='num_zero')
        false_negative = tf.where(
            empty_fg, 0., tf.to_float(tf.truediv(num_zero, num_fg)), name='false_negative')
        fg_accuracy = tf.where(
            empty_fg, 0., tf.reduce_mean(tf.gather(correct, fg_inds)), name='fg_accuracy')

    box_loss = tf.where(empty_fg, 0.,
                        tf.truediv(
                            tf.losses.huber_loss(
                                fg_boxes, fg_box_logits, reduction=tf.losses.Reduction.SUM),
                            tf.to_float(tf.shape(labels)[0])),
                        name='box_loss'
                        )

    # box_loss = tf.losses.huber_loss(
    #     fg_boxes, fg_box_logits, reduction=tf.losses.Reduction.SUM)
    # box_loss = tf.truediv(
    #     box_loss, tf.to_float(tf.shape(labels)[0]), name='box_loss')

    # iou loss : smooth l1 loss
    iou_logits = tf.nn.sigmoid(iou_logits)
    iou_loss = tf.losses.huber_loss(iou_labels, iou_logits) * 5.
    iou_loss = tf.identity(iou_loss, name='iou_loss')

    add_moving_summary(label_loss, box_loss, accuracy, iou_loss,
                       fg_accuracy, false_negative, tf.to_float(num_fg, name='num_fg_label'))
    return label_loss, box_loss, iou_loss


@under_name_scope()
def fastrcnn_predictions(boxes, scores):
    """
    Generate final results from predictions of all proposals.

    Args:
        boxes: n#classx4 floatbox in float32
        scores: nx#class

    Returns:
        boxes: Kx4
        scores: K
        labels: K
    """
    assert boxes.shape[1] == cfg.DATA.NUM_CLASS
    assert scores.shape[1] == cfg.DATA.NUM_CLASS
    boxes = tf.transpose(boxes, [1, 0, 2])[1:, :, :]  # #catxnx4
    boxes.set_shape([None, cfg.DATA.NUM_CATEGORY, None])
    scores = tf.transpose(scores[:, 1:], [1, 0])  # #catxn

    def f(X):
        """
        prob: n probabilities
        box: nx4 boxes

        Returns: n boolean, the selection
        """
        prob, box = X
        output_shape = tf.shape(prob)
        # filter by score threshold
        ids = tf.reshape(tf.where(prob > cfg.TEST.RESULT_SCORE_THRESH), [-1])
        prob = tf.gather(prob, ids)
        box = tf.gather(box, ids)
        # NMS within each class
#        selection = tf.image.non_max_suppression(
#            box, prob, cfg.TEST.RESULTS_PER_IM, cfg.TEST.FRCNN_NMS_THRESH)
#        selection = tf.to_int32(tf.gather(ids, selection))
#        # sort available in TF>1.4.0
#        # sorted_selection = tf.contrib.framework.sort(selection, direction='ASCENDING')
#        sorted_selection = -tf.nn.top_k(-selection, k=tf.size(selection))[0]
#        mask = tf.sparse_to_dense(
#            sparse_indices=sorted_selection,
#            output_shape=output_shape,
#            sparse_values=True,
#            default_value=False)
        mask = tf.sparse_to_dense(
            sparse_indices=tf.to_int32(ids),
            output_shape=output_shape,
            sparse_values=True,
            default_value=False)
        return mask

    masks = tf.map_fn(f, (scores, boxes), dtype=tf.bool,
                      parallel_iterations=10)  # #cat x N
    selected_indices = tf.where(masks)  # #selection x 2, each is (cat_id, box_id)
    scores = tf.boolean_mask(scores, masks)

    # filter again by sorting scores
    topk_scores, topk_indices = tf.nn.top_k(
        scores,
        tf.minimum(cfg.TEST.RESULTS_PER_IM, tf.size(scores)),
        sorted=False)
    filtered_selection = tf.gather(selected_indices, topk_indices)
    cat_ids, box_ids = tf.unstack(filtered_selection, axis=1)

    final_scores = tf.identity(topk_scores, name='scores')
    final_labels = tf.add(cat_ids, 1, name='labels')
    final_ids = tf.stack([cat_ids, box_ids], axis=1, name='all_ids')
    final_boxes = tf.gather_nd(boxes, final_ids, name='boxes')
    return final_boxes, final_scores, final_labels


@under_name_scope()
def fastrcnn_predictions_iou(boxes, scores, ious):
    """
    Generate final results from predictions of all proposals.

    Args:
        boxes: n#classx4 floatbox in float32
        scores: nx#class
        ious: nx#class

    Returns:
        boxes: Kx4
        scores: K
        labels: K
    """
    assert boxes.shape[1] == cfg.DATA.NUM_CLASS
    assert scores.shape[1] == cfg.DATA.NUM_CLASS
    assert ious.shape[1] == cfg.DATA.NUM_CLASS

    boxes = tf.transpose(boxes, [1, 0, 2])[1:, :, :]  # #catxnx4
    boxes.set_shape([None, cfg.DATA.NUM_CATEGORY, None])
    scores = tf.transpose(scores[:, 1:], [1, 0])  # #catxn
    ious = tf.transpose(ious[:, 1:], [1, 0])  # #catxn

    def f(X):
        """
        prob: n probabilities
        box: nx4 boxes
        iou: n

        Returns: n boolean, the selection
        """
        prob, box, iou = X
        output_shape = tf.shape(prob)
        # filter by score+iou threshold

        logit_and = tf.logical_and(prob > cfg.TEST.RESULT_SCORE_THRESH, iou > cfg.TEST.RESULT_SCORE_THRESH)
        ids = tf.reshape(tf.where(logit_and), [-1])
        # ids = tf.reshape(tf.where(prob > cfg.TEST.RESULT_SCORE_THRESH), [-1])
        prob = tf.gather(prob, ids)
        box = tf.gather(box, ids)
        iou = tf.gather(iou, ids)

        prob_iou = prob + iou

        # NMS within each class
        selection = tf.image.non_max_suppression(
            box, prob_iou, cfg.TEST.RESULTS_PER_IM, cfg.TEST.FRCNN_NMS_THRESH)
        selection = tf.to_int32(tf.gather(ids, selection))
        # sort available in TF>1.4.0
        # sorted_selection = tf.contrib.framework.sort(selection, direction='ASCENDING')
        sorted_selection = -tf.nn.top_k(-selection, k=tf.size(selection))[0]
        mask = tf.sparse_to_dense(
            sparse_indices=sorted_selection,
            output_shape=output_shape,
            sparse_values=True,
            default_value=False)
#        mask = tf.sparse_to_dense(
#            sparse_indices=tf.to_int32(ids),
#            output_shape=output_shape,
#            sparse_values=True,
#            default_value=False)
        return mask

    masks = tf.map_fn(f, (scores, boxes, ious), dtype=tf.bool,
                      parallel_iterations=10)  # #cat x N
    selected_indices = tf.where(masks)  # #selection x 2, each is (cat_id, box_id)
    scores = tf.boolean_mask(scores, masks)
    ious = tf.boolean_mask(ious, masks)

    scores_ious = scores + ious

    # filter again by sorting scores
    scores_ious, topk_indices = tf.nn.top_k(
        scores_ious,
        tf.minimum(cfg.TEST.RESULTS_PER_IM, tf.size(scores)),
        sorted=False)
    filtered_selection = tf.gather(selected_indices, topk_indices)
    cat_ids, box_ids = tf.unstack(filtered_selection, axis=1)

    topk_scores = tf.gather(scores, topk_indices)
    topk_ious = tf.gather(ious, topk_indices)
    final_scores = tf.identity(topk_scores, name='scores')
    final_ious = tf.identity(topk_ious, name='ious')
    final_labels = tf.add(cat_ids, 1, name='labels')
    final_ids = tf.stack([cat_ids, box_ids], axis=1, name='all_ids')
    final_boxes = tf.gather_nd(boxes, final_ids, name='boxes')
    
#    cat_ids, box_ids = tf.unstack(selected_indices, axis=1)
#    final_scores = tf.identity(scores, name='scores')
#    final_ious = tf.identity(ious, name='ious')
#    final_labels = tf.add(cat_ids, 1, name='labels')
#    final_ids = tf.stack([cat_ids, box_ids], axis=1, name='all_ids')
#    final_boxes = tf.gather_nd(boxes, final_ids, name='boxes')

    return final_boxes, final_scores, final_labels, final_ious


"""
FastRCNN heads for FPN:
"""


@layer_register(log_shape=True)
def fastrcnn_2fc_head(feature):
    """
    Args:
        feature (any shape):

    Returns:
        2D head feature
    """
    dim = cfg.FPN.FRCNN_FC_HEAD_DIM
    init = tf.variance_scaling_initializer()
    hidden = FullyConnected('fc6', feature, dim, kernel_initializer=init, activation=tf.nn.relu)
    hidden = FullyConnected('fc7', hidden, dim, kernel_initializer=init, activation=tf.nn.relu)
    return hidden


@layer_register(log_shape=True)
def fastrcnn_Xconv1fc_head(feature, num_convs, norm=None):
    """
    Args:
        feature (NCHW):
        num_classes(int): num_category + 1
        num_convs (int): number of conv layers
        norm (str or None): either None or 'GN'

    Returns:
        2D head feature
    """
    assert norm in [None, 'GN'], norm
    l = feature
    with argscope(Conv2D, data_format='channels_first',
                  kernel_initializer=tf.variance_scaling_initializer(
                      scale=2.0, mode='fan_out', distribution='normal')):
        for k in range(num_convs):
            l = Conv2D('conv{}'.format(k), l, cfg.FPN.FRCNN_CONV_HEAD_DIM, 3, activation=tf.nn.relu)
            if norm is not None:
                l = GroupNorm('gn{}'.format(k), l)
        l = FullyConnected('fc', l, cfg.FPN.FRCNN_FC_HEAD_DIM,
                           kernel_initializer=tf.variance_scaling_initializer(), activation=tf.nn.relu)
    return l


def fastrcnn_4conv1fc_head(*args, **kwargs):
    return fastrcnn_Xconv1fc_head(*args, num_convs=4, **kwargs)


def fastrcnn_4conv1fc_gn_head(*args, **kwargs):
    return fastrcnn_Xconv1fc_head(*args, num_convs=4, norm='GN', **kwargs)


class BoxProposals(object):
    """
    A structure to manage box proposals and their relations with ground truth.
    """

    def __init__(self, boxes,
                 labels=None, fg_inds_wrt_gt=None,
                 gt_boxes=None, gt_labels=None):
        """
        Args:
            boxes: Nx4
            labels: N, each in [0, #class), the true label for each input box
            fg_inds_wrt_gt: #fg, each in [0, M)
            gt_boxes: Mx4
            gt_labels: M

        The last four arguments could be None when not training.
        """
        for k, v in locals().items():
            if k != 'self' and v is not None:
                setattr(self, k, v)

    @memoized
    def fg_inds(self):
        """ Returns: #fg indices in [0, N-1] """
        return tf.reshape(tf.where(self.labels > 0), [-1], name='fg_inds')

    @memoized
    def fg_boxes(self):
        """ Returns: #fg x4"""
        return tf.gather(self.boxes, self.fg_inds(), name='fg_boxes')

    @memoized
    def fg_labels(self):
        """ Returns: #fg"""
        return tf.gather(self.labels, self.fg_inds(), name='fg_labels')

    @memoized
    def matched_gt_boxes(self):
        """ Returns: #fg x 4"""
        return tf.gather(self.gt_boxes, self.fg_inds_wrt_gt)


class FastRCNNHead(object):
    """
    A class to process & decode inputs/outputs of a fastrcnn classification+regression head.
    """

    def __init__(self, proposals, box_logits, label_logits, bbox_regression_weights):
        """
        Args:
            proposals: BoxProposals
            box_logits: Nx#classx4 or Nx1x4, the output of the head
            label_logits: Nx#class, the output of the head
            bbox_regression_weights: a 4 element tensor
        """
        for k, v in locals().items():
            if k != 'self' and v is not None:
                setattr(self, k, v)
        self._bbox_class_agnostic = int(box_logits.shape[1]) == 1

    @memoized
    def fg_box_logits(self):
        """ Returns: #fg x ? x 4 """
        return tf.gather(self.box_logits, self.proposals.fg_inds(), name='fg_box_logits')

    @memoized
    def losses(self):
        encoded_fg_gt_boxes = encode_bbox_target(
            self.proposals.matched_gt_boxes(),
            self.proposals.fg_boxes()) * self.bbox_regression_weights
        return fastrcnn_losses(
            self.proposals.labels, self.label_logits,
            encoded_fg_gt_boxes, self.fg_box_logits()
        )

    @memoized
    def decoded_output_boxes(self):
        """ Returns: N x #class x 4 """
        anchors = tf.tile(tf.expand_dims(self.proposals.boxes, 1),
                          [1, cfg.DATA.NUM_CLASS, 1])  # N x #class x 4
        decoded_boxes = decode_bbox_target(
            self.box_logits / self.bbox_regression_weights,
            anchors
        )
        return decoded_boxes

    @memoized
    def decoded_output_boxes_for_true_label(self):
        """ Returns: Nx4 decoded boxes """
        return self._decoded_output_boxes_for_label(self.proposals.labels)

    @memoized
    def decoded_output_boxes_for_predicted_label(self):
        """ Returns: Nx4 decoded boxes """
        return self._decoded_output_boxes_for_label(self.predicted_labels())

    @memoized
    def decoded_output_boxes_for_label(self, labels):
        assert not self._bbox_class_agnostic
        indices = tf.stack([
            tf.range(tf.size(labels, out_type=tf.int64)),
            labels
        ])
        needed_logits = tf.gather_nd(self.box_logits, indices)
        decoded = decode_bbox_target(
            needed_logits / self.bbox_regression_weights,
            self.proposals.boxes
        )
        return decoded

    @memoized
    def decoded_output_boxes_class_agnostic(self):
        """ Returns: Nx4 """
        assert self._bbox_class_agnostic
        box_logits = tf.reshape(self.box_logits, [-1, 4])
        decoded = decode_bbox_target(
            box_logits / self.bbox_regression_weights,
            self.proposals.boxes
        )
        return decoded

    @memoized
    def output_scores(self, name=None):
        """ Returns: N x #class scores, summed to one for each box."""
        return tf.nn.softmax(self.label_logits, name=name)

    @memoized
    def predicted_labels(self):
        """ Returns: N ints """
        return tf.argmax(self.label_logits, axis=1, name='predicted_labels')


class FastRCNNHead_iou(object):
    """
    A class to process & decode inputs/outputs of a fastrcnn classification+regression head.
    """

    def __init__(self, proposals, box_logits, label_logits, iou_logits, bbox_regression_weights):
        """
        Args:
            proposals: BoxProposals
            box_logits: Nx#classx4 or Nx1x4, the output of the head
            label_logits: Nx#class, the output of the head
            iou_logits: Nx1, the output of the head
            bbox_regression_weights: a 4 element tensor
        """
        for k, v in locals().items():
            if k != 'self' and v is not None:
                setattr(self, k, v)
        self._bbox_class_agnostic = int(box_logits.shape[1]) == 1

    @memoized
    def fg_box_logits(self):
        """ Returns: #fg x ? x 4 """
        return tf.gather(self.box_logits, self.proposals.fg_inds(), name='fg_box_logits')

    @memoized
    def losses(self):
        encoded_fg_gt_boxes = encode_bbox_target(
            self.proposals.matched_gt_boxes(),
            self.proposals.fg_boxes()) * self.bbox_regression_weights

        decoded_boxes = self.decoded_output_boxes()
        decoded_boxes = tf.reshape(decoded_boxes, [-1, 4])
        gt_boxes = tf.reshape(self.proposals.gt_boxes, [-1, 4])
        iou = pairwise_iou(decoded_boxes, gt_boxes)
        max_iou = tf.reduce_max(iou, axis=1)
        # if only bg gt_boxes, all ious are 0.
        pos_mask = tf.stop_gradient(tf.not_equal(self.proposals.labels, 0))
        nr_pos = tf.identity(tf.count_nonzero(pos_mask, dtype=tf.int32))
        max_iou = tf.where(tf.equal(nr_pos, 0), tf.zeros_like(max_iou), max_iou)
        max_iou = tf.stop_gradient(tf.reshape(max_iou, [-1]))

        return fastrcnn_losses_iou(
            self.proposals.labels, self.label_logits,
            max_iou, tf.reshape(self.iou_logits, [-1]),
            encoded_fg_gt_boxes, self.fg_box_logits()
        )

    @memoized
    def decoded_output_boxes(self):
        """ Returns: N x #class x 4 """
        anchors = tf.tile(tf.expand_dims(self.proposals.boxes, 1),
                          [1, cfg.DATA.NUM_CLASS, 1])  # N x #class x 4
        decoded_boxes = decode_bbox_target(
            self.box_logits / self.bbox_regression_weights,
            anchors
        )
        return decoded_boxes

    @memoized
    def decoded_output_boxes_for_true_label(self):
        """ Returns: Nx4 decoded boxes """
        return self._decoded_output_boxes_for_label(self.proposals.labels)

    @memoized
    def decoded_output_boxes_for_predicted_label(self):
        """ Returns: Nx4 decoded boxes """
        return self._decoded_output_boxes_for_label(self.predicted_labels())

    @memoized
    def decoded_output_boxes_for_label(self, labels):
        assert not self._bbox_class_agnostic
        indices = tf.stack([
            tf.range(tf.size(labels, out_type=tf.int64)),
            labels
        ])
        needed_logits = tf.gather_nd(self.box_logits, indices)
        decoded = decode_bbox_target(
            needed_logits / self.bbox_regression_weights,
            self.proposals.boxes
        )
        return decoded

    @memoized
    def decoded_output_boxes_class_agnostic(self):
        """ Returns: Nx4 """
        assert self._bbox_class_agnostic
        box_logits = tf.reshape(self.box_logits, [-1, 4])
        decoded = decode_bbox_target(
            box_logits / self.bbox_regression_weights,
            self.proposals.boxes
        )
        return decoded

    @memoized
    def output_scores(self, name=None):
        """ Returns: N x #class scores, summed to one for each box."""
        return tf.nn.softmax(self.label_logits, name=name)

    @memoized
    def output_ious(self, name=None):
        """ Returns: N x #class iou, summed to one for each box."""
        return tf.nn.sigmoid(self.iou_logits, name=name)

    @memoized
    def predicted_labels(self):
        """ Returns: N ints """
        return tf.argmax(self.label_logits, axis=1, name='predicted_labels')
