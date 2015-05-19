# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

import datasets
import datasets.nyud2_voc
import os
import datasets.imdb
import xml.dom.minidom as minidom
import numpy as np
import scipy.sparse
import scipy.io as sio
import utils.cython_bbox
import cPickle
import subprocess
from IPython.core.debugger import Tracer

class nyud2_voc(datasets.imdb):
    def __init__(self, image_set, year, devkit_path=None, image_type = 'images'):
        datasets.imdb.__init__(self, 'nyud2_' + image_type + '_' + year + '_' + image_set)
        self._year = year
        self._image_set = image_set
        self._devkit_path = self._get_default_path() if devkit_path is None \
                            else devkit_path
        self._data_path = os.path.join(self._devkit_path, 'data')
        
        self._classes = ('__background__', # always index 0
            'bathtub', 'bed', 'bookshelf', 'box', 'chair', 'counter', 'desk',
            'door', 'dresser', 'garbage-bin', 'lamp', 'monitor', 'night-stand',
            'pillow', 'sink', 'sofa', 'table', 'television', 'toilet');

        self._class_to_ind = dict(zip(self.classes, xrange(self.num_classes)))
        self._image_type = image_type;
        self._image_ext = '.png'
        self._image_index = self._load_image_set_index()
        # Default to roidb handler
        self._roidb_handler = self.mcg_roidb

        # PASCAL specific config options
        self.config = {'cleanup'  : True,
                       'use_salt' : True,
                       'top_k'    : 2000}

        assert os.path.exists(self._devkit_path), \
                'VOCdevkit path does not exist: {}'.format(self._devkit_path)
        assert os.path.exists(self._data_path), \
                'Path does not exist: {}'.format(self._data_path)

    def image_path_at(self, i):
        """
        Return the absolute path to image i in the image sequence.
        """
        return self.image_path_from_index(self._image_index[i])

    def image_path_from_index(self, index):
        """
        Construct an image path from the image's "index" identifier.
        """
        image_path = os.path.join(self._data_path, self._image_type,
                                  index + self._image_ext)
        assert os.path.exists(image_path), \
                'Path does not exist: {}'.format(image_path)
        return image_path

    def _load_image_set_index(self):
        """
        Load the indexes listed in this dataset's image set file.
        """
        # Example path to image set file:
        # self._devkit_path + /VOCdevkit2007/VOC2007/ImageSets/Main/val.txt
        image_set_file = os.path.join(self._devkit_path, 'benchmarkData', 'metadata',
                                      'nyusplits.mat')
        assert os.path.exists(image_set_file), \
                'Path does not exist: {}'.format(image_set_file)
        raw_data = sio.loadmat(image_set_file)[self._image_set].ravel()
        image_index = ['img_{:4d}'.format(i) for i in raw_data]
        
        return image_index

    def _get_default_path(self):
        """
        Return the default path where PASCAL VOC is expected to be installed.
        """
        return os.path.join('/work4', 'sgupta', 'datasets', 'nyud2')

    def gt_roidb(self):
        """
        Return the database of ground-truth regions of interest.

        This function loads/saves from/to a cache file to speed up future calls.
        """
        cache_file = os.path.join(self.cache_path, self.name + '_gt_roidb.pkl')
        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as fid:
                roidb = cPickle.load(fid)
            print '{} gt roidb loaded from {}'.format(self.name, cache_file)
            return roidb

        gt_roidb = [self._load_nyud2_annotation(index)
                    for index in self.image_index]
        with open(cache_file, 'wb') as fid:
            cPickle.dump(gt_roidb, fid, cPickle.HIGHEST_PROTOCOL)
        print 'wrote gt roidb to {}'.format(cache_file)

        return gt_roidb

    def mcg_roidb(self):
        """
        Return the database of selective search regions of interest.
        Ground-truth ROIs are also included.

        This function loads/saves from/to a cache file to speed up future calls.
        """
        cache_file = os.path.join(self.cache_path,
                                  self.name + '_mcg_roidb.pkl')

        if os.path.exists(cache_file):
            with open(cache_file, 'rb') as fid:
                roidb = cPickle.load(fid)
            print '{} ss roidb loaded from {}'.format(self.name, cache_file)
            return roidb

        if True:
            gt_roidb = self.gt_roidb()
            ss_roidb = self._load_mcg_roidb(gt_roidb)
            roidb = datasets.imdb.merge_roidbs(gt_roidb, ss_roidb)
        else:
            roidb = self._load_mcg_roidb(None)
        with open(cache_file, 'wb') as fid:
            cPickle.dump(roidb, fid, cPickle.HIGHEST_PROTOCOL)
        print 'wrote mcg roidb to {}'.format(cache_file)

        return roidb

    def _load_mcg_roidb(self, gt_roidb):
        filename = os.path.abspath(os.path.join(self.cache_path, '..',
                                                'mcg_data', 'nyud2_2015' + '.mat'))
        assert os.path.exists(filename), \
               'Selective search data not found at: {}'.format(filename)
        boxes = sio.loadmat(filename)['bboxes'].ravel()
        imnames = sio.loadmat(filename)['imnames'].ravel()
        imnames = [str(x[0]) for x in imnames]
        
        box_list = []
        for i in xrange(len(self._image_index)):
            ind = np.where(self._image_index[i] == np.array(imnames))[0]
            assert(len(ind) == 1)
            box_list.append(boxes[i][:, (1, 0, 3, 2)] - 1)

        return self.create_roidb_from_box_list(box_list, gt_roidb)

    def _load_nyud2_annotation(self, index):
        """
        Load image and bounding boxes info from XML file in the PASCAL VOC
        format.
        """
        filename = os.path.join(self._devkit_path, 'benchmarkData', \
            'gt_box_cache_dir', index + '.mat')
        print 'Loading: {}'.format(filename)
        raw_data = sio.loadmat(filename)
        objs = raw_data['rec']['objects'][0][0][0]

        # Select object we care about
        objs = [obj for obj in objs if self._class_to_ind.get(str(obj['class'][0])) is not None]
        
        num_objs = len(objs)

        boxes = np.zeros((num_objs, 4), dtype=np.uint16)
        gt_classes = np.zeros((num_objs), dtype=np.int32)
        overlaps = np.zeros((num_objs, self.num_classes), dtype=np.float32)

        # Load object bounding boxes into a data frame.
        for ix, obj in enumerate(objs):
            # Make pixel indexes 0-based
            cls = self._class_to_ind.get(str(obj['class'][0]))
            boxes[ix, :] = obj['bbox'][0] - 1
            gt_classes[ix] = cls
            overlaps[ix, cls] = 1.0

        overlaps = scipy.sparse.csr_matrix(overlaps)

        return {'boxes' : boxes,
                'gt_classes': gt_classes,
                'gt_overlaps' : overlaps,
                'flipped' : False}

    def evaluate_detections(self, all_boxes, output_dir):
        comp_id = self._write_voc_results_file(all_boxes)
        self._do_matlab_eval(comp_id, output_dir)

if __name__ == '__main__':
    d = datasets.nyud2_voc('trainval', '2007')
    # res = d.roidb
    from IPython import embed; embed()
