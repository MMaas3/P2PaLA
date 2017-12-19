from __future__ import print_function
from __future__ import division

import os
import glob
import logging
import errno

import numpy as np
import cv2
from multiprocessing import Pool
import itertools
try:
    import cPickle as pickle
except:
    import pickle #--- To handle data export

from page_xml.xmlPAGE import pageData
from utils import polyapprox as pa

#--- TODO: add logging to _pre_process function

class htrDataProcess():
    """
    """
    def __init__(self,data_pointer, out_size, out_folder, classes,
                 line_width=10, line_color=128, processes=2,
                 approx_alg=None, num_segments=4,
                 build_labels=True, only_lines=False, logger=None):
        """ function to proces all data into a htr dataset"""
        self.logger = logging.getLogger(__name__) if logger==None else logger 
        #--- file formats from opencv imread supported formats
        #--- any issue see: https://docs.opencv.org/3.0-beta/modules/imgcodecs/doc/reading_and_writing_images.html#imread
        self.formats = ['tif','tiff', 'png', 'jpg', 'jpeg', 'JPG','bmp']
        self.data_pointer = data_pointer
        self.out_size = out_size
        self.out_folder = out_folder
        self.classes = classes
        self.th_span = 64 if only_lines else (classes[classes.keys()[1]]-classes[classes.keys()[0]])/2 
        self.line_width = line_width
        self.line_color = line_color
        self.processes = processes
        self.approx_alg = approx_alg
        self.num_segments = num_segments
        self.build_labels = build_labels
        self.only_lines = only_lines
        #--- Create output folder if not exist
        if not os.path.exists(self.out_folder):
            self.logger.debug('Creating {} folder...'.format(self.out_folder))
            os.makedirs(self.out_folder)
        self.img_paths = []

        for ext in self.formats:
            self.img_paths.extend(glob.glob(self.data_pointer + '/*.' + ext))
        img_ids = [os.path.splitext(os.path.basename(x))[0] for x in self.img_paths]
        self.img_data = dict(zip(img_ids,self.img_paths))
    
    def pre_process(self):
        """
        """
        self.processed_data = []
        try:
            pool = Pool(processes=self.processes) #--- call without parameters = Pool(processes=cpu_count())
            l_list = len(self.img_paths)
            params = itertools.izip(self.img_paths,[self.out_size]*l_list,
                               [self.out_folder]*l_list,
                               [self.classes]*l_list,
                               [self.line_width]*l_list,
                               [self.line_color]*l_list,
                               [self.build_labels]*l_list,
                               [self.only_lines]*l_list)
            #--- keep _processData out of the class in order to be pickable
            #--- Pool do not support not pickable objects
            #--- TODO: move func inside the class, and pass logger to it
            self.processed_data = pool.map(_processData,params)
        except Exception as e:
            pool.close()
            pool.terminate()
            self.logger.error(e)
        else:
            pool.close()
            pool.join()
        self.processed_data = np.array(self.processed_data)
        np.savetxt(self.out_folder + '/img.lst',self.processed_data[:,0],fmt='%s')
        if self.build_labels:
            np.savetxt(self.out_folder + '/label.lst',self.processed_data[:,1],fmt='%s')
            np.savetxt(self.out_folder + '/label_w.lst',self.processed_data[:,2],fmt='%s')
            self.label_list = self.out_folder + '/label.lst'
            self.w_list = self.out_folder + '/label_w.lst'
        self.img_list = self.out_folder + '/img.lst'

    def gen_page(self,img_id,data, reg_list=None, out_folder='./',
                 approx_alg=None, num_segments=None):
        """
        """
        self.approx_alg = self.approx_alg if approx_alg==None else approx_alg
        self.num_segments = self.num_segments if num_segments==None else num_segments
        self.logger.debug('Gen PAGE for image: {}'.format(img_id))
        #--- sym link to original image 
        img_name = os.path.basename(self.img_data[img_id])
        symlink_force(os.path.realpath(self.img_data[img_id]),
                      os.path.join(out_folder,img_name))
        o_img = cv2.imread(self.img_data[img_id])
        (o_rows, o_cols, _) = o_img.shape
        o_max = max(o_rows,o_cols)
        o_min = min(o_rows,o_cols)
        cScale = np.array([o_cols/self.out_size[1],
                           o_rows/self.out_size[0]])
        
        page = pageData(os.path.join(out_folder, 'page', img_id + '.xml'),
                        logger=self.logger)
        page.new_page(img_name, str(o_rows), str(o_cols)) 
        if self.only_lines:
            l_data = data[0]
            reg_list = ['full_page']
            colors = {'full_page':128}
            r_data = np.zeros(l_data.shape,dtype='uint8')
        else:
            l_data = data[0]
            r_data = data[1]
            colors = self.classes
        lines = np.zeros(l_data.shape,dtype='uint8')
        #--- data comes on [-1, 1] range, but colors are in [0,255]
        #--- apply threshold over two class layeri, bg=-1
        l_color = (-1 - ((self.line_color*(2/255))-1))/2
        lines[l_data > l_color] = 1
        reg_mask = np.zeros(l_data.shape,dtype='uint8')
        lin_mask = np.zeros(l_data.shape,dtype='uint8')
        r_id = 0
        kernel = np.ones((5,5),np.uint8)

        #--- get regions and lines for each class
        for reg in reg_list:
            r_color = colors[reg]
            #--- fill the array is faster then create a new one or mult by 0
            reg_mask.fill(0)
            lim_inf = ((r_color - self.th_span)*(2/255)) - 1
            lim_sup = ((r_color + self.th_span)*(2/255)) - 1
            reg_mask[np.where((r_data > lim_inf) & (r_data < lim_sup))] = 1
            _ , contours, hierarchy = cv2.findContours(reg_mask,
                                                   cv2.RETR_EXTERNAL,
                                                   cv2.CHAIN_APPROX_SIMPLE)
            for cnt in contours:
                #--- remove small objects
                if(cnt.shape[0] < 4):
                    continue
                if(cv2.contourArea(cnt) < 0.1*self.out_size[0]):
                    continue
                #--- get lines inside the region
                lin_mask.fill(0)
                rect = cv2.minAreaRect(cnt)
                #--- soft a bit the region to prevent spikes 
                epsilon = 0.005*cv2.arcLength(cnt,True)
                approx = cv2.approxPolyDP(cnt,epsilon,True)
                #box = np.array((rect[0][0], rect[0][1], rect[1][0], rect[1][1])).astype(int)
                r_id = r_id + 1
                approx= (approx*cScale).astype('int32')
                reg_coords = ''
                for x in approx.reshape(-1,2):
                    reg_coords = reg_coords + " {},{}".format(x[0],x[1])

                cv2.fillConvexPoly(lin_mask,points=cnt, color=(1,1,1))
                lin_mask = cv2.erode(lin_mask,kernel,iterations = 1)
                lin_mask = cv2.dilate(lin_mask,kernel,iterations = 1)
                reg_lines = lines * lin_mask
                #--- search for the lines
                _, l_cont, l_hier = cv2.findContours(reg_lines,
                                                  cv2.RETR_EXTERNAL,
                                                  cv2.CHAIN_APPROX_SIMPLE)
                if (len(l_cont) == 0):
                    continue
                #--- Add region to XML only is there is some line
                text_reg = page.add_element('TextRegion',
                                            str(r_id),
                                            reg,
                                            reg_coords.strip())
                n_lines = 0
                for l_id,l_cnt in enumerate(l_cont):
                    if(l_cnt.shape[0] < 4):
                        continue
                    if (cv2.contourArea(l_cnt) < 0.1*self.out_size[0]):
                        continue
                    #--- convert to convexHull if poly is not convex
                    if (not cv2.isContourConvex(l_cnt)):
                        l_cnt = cv2.convexHull(l_cnt)
                    lin_coords = ''
                    l_cnt = (l_cnt*cScale).astype('int32')
                    for l_x in l_cnt.reshape(-1,2): 
                        lin_coords = lin_coords + " {},{}".format(l_x[0],l_x[1])
                    (is_line, approx_lin) = self._get_baseline(o_img, l_cnt)
                    if is_line == False:
                        continue
                    text_line = page.add_element('TextLine',
                                                 str(l_id) + '_' + str(r_id),
                                                 reg,
                                                 lin_coords.strip(),
                                                 parent=text_reg)
                    baseline = pa.points_to_str(approx_lin)
                    page.add_baseline(baseline, text_line)
                    n_lines += 1
                #--- remove regions without text lines
                if n_lines == 0:
                    page.remove_element(text_reg)
        page.save_xml()

    def _get_baseline(self,Oimg, Lpoly):
        """
        """
        #--- Oimg = image to find the line
        #--- Lpoly polygon where the line is expected to be
        minX = Lpoly[:,:,0].min()
        maxX = Lpoly[:,:,0].max()
        minY = Lpoly[:,:,1].min()
        maxY = Lpoly[:,:,1].max()
        mask = np.zeros(Oimg.shape, dtype=np.uint8)
        cv2.fillConvexPoly(mask,Lpoly, (255,255,255))
        res = cv2.bitwise_and(Oimg,mask)
        bRes = Oimg[minY:maxY, minX:maxX]
        bMsk = mask[minY:maxY, minX:maxX]
        bRes = cv2.cvtColor( bRes, cv2.COLOR_RGB2GRAY )
        _, bImg = cv2.threshold(bRes,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
        _,cols = bImg.shape
        #--- remove black halo around the image
        bImg[bMsk[:,:,0]==0] = 255
        Cs= np.cumsum(abs(bImg-255), axis=0)
        maxPoints=np.argmax(Cs, axis=0)
        Lmsk = np.zeros(bImg.shape)
        points = np.zeros((cols,2), dtype='int')
        #--- gen a 2D list of points
        for i,j in enumerate(maxPoints):
            points[i,:] = [i,j]
        #--- remove points at poss 0, there are high porbable to be blank spaces
        points2D = points[points[:,1]>0]
        if (points2D.size == 0):
            #--- there is no real line
            return (False, [[0,0]])
        if self.approx_alg == 'optimal':
            #--- take only 100 points to build the baseline 
            points2D = points2D[np.linspace(0,points2D.shape[0]-1,100,dtype=np.int)]
            (approxError, approxLin) = pa.poly_approx(points2D,
                                                  self.num_segments,
                                                  pa.one_axis_delta)
        elif self.approx_alg == 'trace':
            approxLin = pa.norm_trace(points2D, self.num_segments)
        else:
            approxLin = points2D
        approxLin[:,0] = approxLin[:,0] + minX
        approxLin[:,1] = approxLin[:,1] + minY
        return (True,approxLin)


#---- misc functions to this class

def _processData(params):
    """
    Resize image and extract mask from PAGE file 
    """
    (img_path,out_size,out_folder,classes,
        line_width,line_color,build_labels, only_lines) = params
    img_id = os.path.splitext(os.path.basename(img_path))[0]
    img_dir = os.path.dirname(img_path)

    img_data = cv2.imread(img_path)
    #--- resize image 
    res_img = cv2.resize(img_data,
                         (out_size[1],out_size[0]),
                         interpolation=cv2.INTER_CUBIC)
    new_img_path = os.path.join(out_folder,img_id+'.png')
    cv2.imwrite(new_img_path,res_img)
    #--- get label
    if build_labels:
        if (os.path.isfile(img_dir + '/page/' + img_id + '.xml')):
            xml_path = img_dir + '/page/' + img_id + '.xml'
        else:
            #logger.critical('No xml found for file {}'.format(img_path))
            #--- TODO move to logger
            print('No xml found for file {}'.format(img_path))
            raise Exception("Execution stop due Critical Errors")
        gt_data = pageData(xml_path)
        gt_data.parse()
        #--- build lines mask
        lin_mask = gt_data.build_baseline_mask(out_size,line_color,line_width)
        unq,idx = np.unique(lin_mask, return_inverse=True)
        f_idx = np.bincount(idx)
        lin_class_norm = (1/f_idx[idx]).reshape(lin_mask.shape)
        #--- buid regions mask
        if not only_lines:
            reg_mask = gt_data.build_mask(out_size,'TextRegion', classes)
            unq,idx = np.unique(reg_mask, return_inverse=True)
            f_idx = np.bincount(idx)
            reg_class_norm = (1/f_idx[idx]).reshape(reg_mask.shape)
            label = np.array((lin_mask,reg_mask))
            label_w = np.array((lin_class_norm,reg_class_norm),dtype=np.float32)
        else:
            label = lin_mask
            label_w = lin_class_norm.astype(np.float32)

        new_label_path = os.path.join(out_folder, img_id + '.pickle')
        new_label_w_path = os.path.join(out_folder, img_id + '_w.pickle')
        fh = open(new_label_path,'w')
        pickle.dump(label,fh,-1)
        fh.close()
        fh = open(new_label_w_path,'w')
        pickle.dump(label_w,fh,-1)
        fh.close()
        return (new_img_path, new_label_path, new_label_w_path)
    return (new_img_path, None, None)

def symlink_force(target, link_name):
    #--- from https://stackoverflow.com/questions/8299386/modifying-a-symlink-in-python
    try:
        os.symlink(target, link_name)
    except OSError as e:
        if e.errno == errno.EEXIST:
            os.remove(link_name)
            os.symlink(target, link_name)
        else:
            raise e



