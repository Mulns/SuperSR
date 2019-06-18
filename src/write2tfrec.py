from tensorflow.python.data import Dataset
import tensorflow as tf
from tqdm import tqdm
from PIL import Image
import numpy as np
import itertools
import random
import glob
import os

AUTOTUNE = tf.data.experimental.AUTOTUNE
feature_name = "data"


def _bytes_feature(value):
    """Returns a bytes_list from a string / byte."""
    if isinstance(value, type(tf.constant(0))):
        value = value.numpy(
        )  # BytesList won't unpack a string from an EagerTensor.
    return tf.train.Feature(bytes_list=tf.train.BytesList(value=[value]))


def write_dst_tfrec(hr_dir, patch_per_image, patch_size, tfrec_path):
    ''' Write patch of hr image into tfrecord file.

        We save all patches with dtype in Uint8, which cropped from Hr-image in RGB color space.

        Params:

            hr_dir: String.
                The directory to Hr images.
            patch_per_image: Int.
                Number of patches per image to save. patches are cropped randomly.
            patch_size: Tuple of integers.
                Size of patch, e.g. (48, 48).
            tfrec_path: String.
                Path to tfrecord file.
    '''

    print('WRITING TO TFRECORD'.center(100, '='))

    H, W = patch_size
    paths = list(glob.glob(os.path.join(hr_dir, "*")))

    def _serialize_generator_():
        '''
        Python generator, yield patch randomly cropped from Hr-image.
        '''
        for p in tqdm(paths):
            img = tf.image.decode_image(tf.io.read_file(p))
            for _ in range(patch_per_imag):
                patch = tf.image.random_crop(img, (H, W, 3))
                data_str = tf.io.serialize_tensor(patch)
                # Create a dictionary mapping the feature name to the tf.Example-compatible
                # data type.
                feature = {feature_name: _bytes_feature(data_str)}
                # Create a Features message using tf.train.Example.
                example_proto = tf.train.Example(
                    features=tf.train.Features(feature=feature))
                yield example_proto.SerializeToString()

    serialize_dst = tf.data.Dataset.from_generator(
        segen, output_types=tf.string)

    writer = tf.data.experimental.TFRecordWriter(tfrec_path)
    writer.write(serialize_dst)


def load_tfrecord(tfrec_file):
    '''Load patches from tfrecord wroten by `write_dst_tfrec`

        Params:
            tfrec_file: String.
                Path to tfrecord file.

        Return: 
            TF-Dataset contains Hr-patches.
    '''
    def _parse_function(example_proto):
        feature_description = {
            feature_name: tf.io.FixedLenFeature(
                [], tf.string, default_value='')
        }
        # Parse the input tf.Example proto using the dictionary above.
        features = tf.io.parse_single_example(example_proto,
                                              feature_description)
        return tf.io.parse_tensor(features[feature_name], out_type=tf.uint8)

    raw_dataset = tf.data.TFRecordDataset(tfrec_file).shuffle(100)
    parsed_dataset = raw_dataset.map(_parse_function, AUTOTUNE)
    return parsed_dataset