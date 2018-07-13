
from keras.models import Model
from keras.layers import Concatenate, Add, Average, Input, Dense, Flatten, BatchNormalization, Activation, LeakyReLU
from keras.layers.core import Lambda
from keras.layers.convolutional import Convolution2D, MaxPooling2D, UpSampling2D, Convolution2DTranspose
from keras.utils.np_utils import to_categorical
import keras.callbacks as callbacks
import keras.optimizers as optimizers
from keras import backend as K
from .advanced import TensorBoardBatch
# from image_utils import Dataset, downsample, merge_to_whole
from .utils import PSNR, psnr, SubpixelConv2D
from .image_utils import merge_to_whole, hr2lr, hr2lr_batch, slice_normal, color_mode_transfer
from .Flow import image_flow_h5
import matplotlib.pyplot as plt
import tensorflow as tf
from PIL import Image
import numpy as np
from scipy import misc
import warnings
import h5py
import sys
import os


class BaseSRModel(object):
    """Base model class of all models for SR.

        This is a base model class for super-resolution, contains creating, training and evaluation of the model. If you want to add some new models based on this, you need to complete the create_model() func. By the way, this model supports data preprocessing in image_utils.py

        Attributes:
            model_name: Name of this model.
            weight_path: Path to save this model, using "./weights/model_name.h5" by default.
            input_size: Size of input data in tuple, e.g. (48, 48, 3).
            channel: Number of channels of input.
            model: Model.

            create_model(): Generate the model, you need to complete this func.
            fit():  
    """

    def __init__(self, model_name, input_size, weight_dir=None):
        """        
            Args:
                model_name: Name of this model.
                input_size: Size of input data in tuple, e.g. (48, 48, 3).
                weight_dir: Directory to save the weights.
        """
        self.model_name = model_name
        if weight_dir:
            self.weight_path = os.path.join(
                weight_dir, "%s.h5" % (self.model_name))
        else:
            self.weight_path = "weights/%s.h5" % (self.model_name)
        self.input_size = input_size
        self.channel = self.input_size[-1]
        self.model = self.create_model(load_weights=False)

    def create_model(self, load_weights=False) -> Model:
        """Create model here, you need to complete this func."""
        init = Input(shape=self.input_size)
        return init

    def fit(self,
            tr_gen,
            val_gen,
            num_train,
            num_val,

            learning_rate=1e-4,
            loss='mse',
            loss_weights=[1],
            nb_epochs=500,
            batch_size=100,

            monitor="val_PSNR",
            visual_graph=True,
            visual_grads=True,
            visual_weight_image=True,

            save_history=True,
            log_dir='./logs') -> Model:
        """Trains the model on data generated by a Python generator batch-by-batch.

            The generator is run in parallel to the model, for efficiency.
            For instance, this allows you to do real-time data augmentation
            on images on CPU in parallel to training your model on GPU.

            Args:
                tr_gen: Generator of train data.
                val_gen: Generator of validation data.
                num_train: Int, the number of training blocks.
                num_val: Int, the number of validation blocks.

                learning_rate: Float or List
                loss: String, like "mse" or "mae" and so on. See doc of Model.fit_generator() for more details.
                nb_epochs: Int or List, number of epoch.
                batch_size: Int, number of blocks per batch.

                visual_grads, visual_graph, visual_weight_image, these three parameters are used to tensorboard. But they're not work yet. Still working on it. If you have any solutions, please folk this repo. #FIXME

                save_history: Bool, whether save the log of training or not. If you want to use tensorboard to visualize the training process, set this as True.
                log_dir: String, if save_history, this directory is used to save the log file for tensorboard.
        """

        if self.model == None:
            self.create_model()


        callback_list = []
        # callback_list.append(HistoryCheckpoint(history_fn))
        callback_list.append(callbacks.ModelCheckpoint(self.weight_path, monitor=monitor,
                                                       save_best_only=True, mode='max', save_weights_only=True, verbose=2))
        if save_history:
            log_dir = os.path.join(log_dir, self.model_name)
            callback_list.append(TensorBoardBatch(log_dir=log_dir, batch_size=batch_size, histogram_freq=1,
                                                  write_grads=visual_grads, write_graph=visual_graph, write_images=visual_weight_image))  # FIXME why these parameters doen't work?

        print('Training model : %s' % (self.model_name))

        num_stage = max([len(x) for x in [learning_rate, nb_epochs] if isinstance(x, (list, tuple))])
        num_stage = 1 if not num_stage else num_stage

        for s in range(num_stage):
            print("Stage %d ------ ..."%(s))
            lr = learning_rate if isinstance(learning_rate, float) else learning_rate[s]
            ep = nb_epochs if isinstance(nb_epochs, int) else nb_epochs[s]
            # adam = optimizers.Nadam()
            adam = optimizers.Adam(lr=lr, beta_1=0.9, beta_2=0.999, epsilon=1e-8)
            self.model.compile(optimizer=adam, loss=loss, metrics=[PSNR], loss_weights=loss_weights)

            self.model.fit_generator(tr_gen,
                                    steps_per_epoch=num_train // batch_size + 1, epochs=ep, callbacks=callback_list,
                                    validation_data=val_gen,
                                    validation_steps=num_val // batch_size + 1, workers=4, pickle_safe=True)
        return self.model


    def fit_batch(self, x, y, learning_rate=1e-4, loss='mse'):
        if self.model == None:
            self.create_model()

        # adam = optimizers.Nadam()
        adam = optimizers.Adam(lr=learning_rate)
        self.model.compile(optimizer=adam, loss=loss, metrics=[PSNR])

        return self.model.train_on_batch(x, y)


    def evaluate(self, image_path, mode="auto", stride_scale=0.5, scale=4, lr_shape=0, save=False, save_name="test", verbose=1, **kargs) -> Model:
        """Evaluate the model on one image. 
            Given the image, we first slice it to blocks, then downsample them to lr_block used for evaluation. The lr_block should share the same size with the input size of the model. Output sr-image will be save if you want. 

            Input:
                image_path: String, path to the target image.
                mode: String of "auto" or "Y", in which Y means the Y-channel of YCbCr mode, and auto means it will take the default mode.
                stride_scale: Float from 0 to 1, the scale of stride to input size. 
                scale: Int, scale_factor of downsampling. Better according to the training data.
                lr_shape: Int or Tuple, if is integer, should be 0 or 1. lr_blocks will be set as scaled size if 0, or as size of hr_block if 1. If is tuple, the lr_block will then be resize to the target size.
                save : Bool, whether to save the sr-image to local or not.
                save_name: String, the name of the sr-image if save. 
                verbose, Int, 0 or 1. Print the psnr if 1, else 0.

            Return:
                Tuple of original image, lr-image and sr-image.
                Psnr of this sample.
        """
        h, _, _ = self.input_size
        if lr_shape == 0:
            hr_size = h*scale
        elif lr_shape == 1:
            hr_size = h
        hr_stride = int(hr_size*stride_scale)

        # Read image.
        img = np.array(Image.open(image_path))
        if mode == "Y":
            img = self._formulate_(color_mode_transfer(img, "YCbCr")[:, :, 0])
        # Slice first.
        hr_block, size_merge = slice_normal(
            img, hr_size, hr_stride, to_array=True, merge=True)
        # Downsample to lr-batch.
        lr_block = self._formulate_(np.array(hr2lr_batch(hr_block, scale, lr_shape)))
        # Generate sr-batch with model.
        sr_block = self.model.predict(np.array(lr_block)/255., verbose=verbose)
        # merge all subimages.
        hr_img = merge_to_whole(hr_block, size_merge, stride=hr_stride)
        lr_img = hr2lr(hr_img.squeeze(), scale=scale, shape=1)
        sr_img = merge_to_whole(sr_block, size_merge, stride=hr_stride)*255.

        if verbose == 1:
            print('PSNR is %f' % (psnr(sr_img/255., hr_img/255.)))
        if save:
            misc.imsave('./example/%s_SR.jpg' % (save_name), sr_img)
        return (hr_img, lr_img, sr_img), psnr(sr_img/255., hr_img/255.)

    def evaluate_batch(self, test_path, mode="auto", scale=4, lr_shape=1, verbose=0, return_list=False, **kargs):
        """Evaluate the psnr of all images in directory. 
            Input:
                test_path: String,  path of target images.
                mode: String of "auto" or "Y", in which Y means the Y-channel of YCbCr mode, and auto means it will take the default mode.
                scale: Int, scale_factor of downsampling. Better according to the training data.
                lr_shape: Int or Tuple, if is integer, should be 0 or 1. lr_blocks will be set as scaled size if 0, or as size of hr_block if 1. If is tuple, the lr_block will then be resize to the target size.
                verbose, int. 0 if show progress. 1 if print psnr of all images.
            Return:
                Float of average psnr. 
                List of psnr of all images.
        """
        PSNR = []
        count = 0
        num_total = len(list(sorted(os.listdir(test_path))))
        for _, _, files in os.walk(test_path):
            for image_name in files:
                # Read in image
                count += 1
                _, psnr = self.evaluate(os.path.join(
                    test_path, image_name), verbose=verbose, scale=scale, lr_shape=lr_shape, mode=mode, **kargs)
                if not verbose:
                    sys.stdout.write(
                        "\r %d/%d image has evaluated." % (count, num_total))
                PSNR.append(psnr)
        ave_psnr = np.sum(PSNR)/float(len(PSNR))
        print('average psnr of test images(whole) in %s is %f. \n' %
              (test_path, ave_psnr))
        if return_list:
            return ave_psnr, PSNR
        else:
            return ave_psnr

    def gen_sr_img(self, image_path, mode, stride_scale, scale, lr_shape, save=False, save_name="test", **kargs):
        pass

    def _formulate_(self, x):
        if len(x.shape) == 2:
            return x[:, :, np.newaxis]
        elif len(x.shape) == 3:
            if not x.shape[-1] in [1, 3, 4]:
                return x[:, :, :, np.newaxis]
        return x
# TODO(mulns): Finish gen_sr_img() func and implete the comments.


class SRCNN(BaseSRModel):
    """
    pass
    """

    def __init__(self, model_type, input_size):
        """
        Input:
            model_type, str, name of this SRCNN-net. 
            input_size, tuple, size of input layer. e.g.(48, 48, 3)
        """
        self.f1 = 9
        self.f2 = 5
        self.f3 = 5

        self.n1 = 64
        self.n2 = 32

        super(SRCNN, self).__init__("SRCNN_"+model_type, input_size)

    def create_model(self, load_weights=False):

        init = super(SRCNN, self).create_model()

        x = Convolution2D(self.n1, (self.f1, self.f1),
                          activation='relu', padding='same', name='level1')(init)
        x = Convolution2D(self.n2, (self.f2, self.f2),
                          activation='relu', padding='same', name='level2')(x)

        out = Convolution2D(self.channel, (self.f3, self.f3),
                            padding='same', name='output')(x)

        model = Model(init, out)

        if load_weights:
            model.load_weights(self.weight_path)
            print("loaded model %s" % (self.model_name))

        self.model = model
        return model


class ResNetSR(BaseSRModel):
    """
    Under test. A little different from original paper. 
    """

    def __init__(self, model_type, input_size, scale):

        self.n = 64  # size of feature. also known as number of filters.
        self.mode = 2
        self.f = 3  # filter size
        # by diff scales comes to diff model structure in upsampling layers.
        self.scale = scale

        super(ResNetSR, self).__init__("ResNetSR_"+model_type, input_size)

    def create_model(self, load_weights=False, nb_residual=5):

        init = super(ResNetSR, self).create_model()

        x0 = Convolution2D(self.n, (self.f, self.f), activation='relu',
                           padding='same', name='sr_res_conv1')(init)

        x = self._residual_block(x0, 1)

        nb_residual = nb_residual-1
        for i in range(nb_residual):
            x = self._residual_block(x, i + 2)

        x = Add()([x, x0])

        if self.scale != 1:
            x = self._upscale_block(x, 1)

        x = Convolution2D(self.channel, (self.f, self.f), activation="linear",
                          padding='same', name='sr_res_conv_final')(x)

        model = Model(init, x)
        if load_weights:
            model.load_weights(self.weight_path, by_name=True)
            print("loaded model %s" % (self.model_name))

        self.model = model
        return model

    def _residual_block(self, ip, id):
        mode = False if self.mode == 2 else None
        channel_axis = 1 if K.image_data_format() == 'channels_first' else -1
        init = ip

        x = Convolution2D(self.n, (self.f, self.f), activation='linear', padding='same',
                          name='sr_res_conv_' + str(id) + '_1')(ip)
        x = BatchNormalization(
            axis=channel_axis, name="sr_res_batchnorm_" + str(id) + "_1")(x, training=mode)
        x = Activation('relu', name="sr_res_activation_" + str(id) + "_1")(x)

        x = Convolution2D(self.n, (self.f, self.f), activation='linear', padding='same',
                          name='sr_res_conv_' + str(id) + '_2')(x)
        x = BatchNormalization(
            axis=channel_axis, name="sr_res_batchnorm_" + str(id) + "_2")(x, training=mode)

        m = Add(name="sr_res_merge_" + str(id))([x, init])

        return m

    def _upscale_block(self, ip, id):
        init = ip
        scale = self.scale

        ps_features = self.channel*(scale**2)
        x = Convolution2D(ps_features, (self.f, self.f), activation='relu',
                          padding='same', name='sr_upsample_conv%d' % (id))(init)
        x = SubpixelConv2D(input_shape=self.input_size, scale=scale)(x)
        return x


class EDSR(BaseSRModel):

    def __init__(self, model_type, input_size, scale):

        self.n = 256  # size of feature. also known as number of filters.
        self.f = 3  # shape of filter. kernel_size
        self.scale_res = 0.1  # used in each residual net
        # by diff scales comes to diff model structure in upsampling layers.
        self.scale = scale
        super(EDSR, self).__init__("EDSR_"+model_type, input_size)

    def create_model(self, load_weights=False, nb_residual=32):

        init = super(EDSR, self).create_model()

        x0 = Convolution2D(self.n, (self.f, self.f), activation='linear',
                           padding='same', name='sr_conv1')(init)

        x = self._residual_block(x0, 1, scale=self.scale_res)

        nb_residual = nb_residual - 1
        for i in range(nb_residual):
            x = self._residual_block(x, i + 2, scale=self.scale_res)

        x = Convolution2D(self.n, (self.f, self.f),
                          activation='linear', padding='same', name='sr_conv2')(x)
        x = Add()([x, x0])

        x = self._upsample(x)

        out = Convolution2D(self.channel, (self.f, self.f),
                            activation="linear", padding='same', name='sr_conv_final')(x)

        model = Model(init, out)

        if load_weights:
            model.load_weights(self.weight_path, skip_mismatch=True)
            print("loaded model %s" % (self.model_name))

        self.model = model
        return model

    def _upsample(self, x):
        scale = self.scale
        assert scale in [2, 3, 4, 8], 'scale should be 2, 3 ,4 or 8!'
        x = Convolution2D(self.n, (self.f, self.f), activation='linear',
                          padding='same', name='sr_upsample_conv1')(x)
        if scale == 2:
            ps_features = self.channel*(scale**2)
            x = Convolution2D(ps_features, (self.f, self.f), activation='relu',
                              padding='same', name='sr_upsample_conv2')(x)
            x = SubpixelConv2D(input_shape=self.input_size, scale=scale)(x)
        elif scale == 3:
            ps_features = self.channel*(scale**2)
            x = Convolution2D(ps_features, (self.f, self.f), activation='linear',
                              padding='same', name='sr_upsample_conv2')(x)
            x = SubpixelConv2D(input_shape=self.input_size, scale=scale)(x)
        elif scale == 4:
            ps_features = self.channel*(2**2)
            for i in range(2):
                x = Convolution2D(ps_features, (self.f, self.f), activation='linear',
                                  padding='same', name='sr_upsample_conv%d' % (i+2))(x)
                x = SubpixelConv2D(
                    input_shape=self.input_size, scale=2, id=i+1)(x)
        elif scale == 8:
            # scale 4 by 2 times or scale 2 by 4 times? under estimate!
            ps_features = self.channel*(4**2)
            for i in range(2):
                x = Convolution2D(ps_features, (self.f, self.f), activation='linear',
                                  padding='same', name='sr_upsample_conv%d' % (i+2))(x)
                x = SubpixelConv2D(
                    input_shape=self.input_size, scale=4, id=i+1)(x)
        return x

    def _residual_block(self, ip, id, scale):

        init = ip

        x = Convolution2D(self.n, (self.f, self.f), activation='relu', padding='same',
                          name='sr_res_conv_' + str(id) + '_1')(ip)
        x = Activation('relu', name="sr_res_activation_" + str(id) + "_1")(x)

        x = Convolution2D(self.n, (self.f, self.f), activation='relu', padding='same',
                          name='sr_res_conv_' + str(id) + '_2')(x)

        Lambda(lambda x: x * self.scale_res)(x)
        m = Add(name="res_merge_" + str(id))([x, init])

        return m



def main():
    # change to yours. See image_utils.py for details.
    trainH5_path = "/media/mulns/F25ABE595ABE1A75/H5File/div2k_RGB_tr_diff_2348X.h5"
    valH5_path = "/media/mulns/F25ABE595ABE1A75/H5File/div2k_RGB_val_diff_2348X.h5"

    # Generator of train data from h5 file.
    tr_gen = image_flow_h5(trainH5_path, batch_size=16,
                           big_batch_size=5000, shuffle=True, index=("lr_2X", "hr"), normalize=True)
    val_gen = image_flow_h5(
        valH5_path, batch_size=16, big_batch_size=5000,
        shuffle=False, index=("lr_2X", "hr"), normalize=True)

    # Model .
    srcnn = SRCNN("div2k", input_size=(48, 48, 3))
    srcnn.create_model(load_weights=True)  # True if weights already exists.
    # edsr = EDSR("div2k_2X", (24,24,3), scale=2)
    # edsr.create_model(load_weights=False)

    # # Train part.
    # edsr.fit(tr_gen, val_gen, num_train=800000,
    #          num_val=100000, batch_size=16, loss="mae")

    # Evaluate part.(Only one sample here)
    imgs, _ = srcnn.evaluate(
        "./test_image/butterfly_GT.bmp", mode="auto", scale=4, lr_shape=1)

    # Save the result.
    misc.imsave("./example/butt_sr_2X.jpg", imgs[1].squeeze())
    misc.imsave("./example/butt_hr.jpg", imgs[0].squeeze())
    misc.imsave("./example/butt_lr_2X.jpg", misc.imresize(imgs[0].squeeze(), 1/3))

    # # Evaluate images from a directory and calculate the average psnr.
    psnr, PSNR_List = srcnn.evaluate_batch(
        "./test_image/set5", mode="auto", scale=3, lr_shape=1, stride_scale=0.5, return_list=True)
    print("Average psnr of this Directory is ", psnr)
    # Average psnr of this Directory(100 images from DIV2K Validation dataset) is  27.549315894072866

    # Plot the sample and psnr.
    plt.subplot(131)
    plt.imshow(np.uint8(imgs[0].squeeze()))
    plt.subplot(132)
    plt.imshow(np.uint8(imgs[1].squeeze()))
    plt.subplot(133)
    plt.plot(np.arange(len(PSNR_List)), PSNR_List)
    plt.show()


if __name__ == '__main__':
    main()
