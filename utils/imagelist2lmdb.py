from os import path

import lmdb
import lz4framed
import numpy as np
import pandas as pd
import pyarrow as pa
from torch.utils.data import DataLoader
from torchvision.datasets import ImageFolder
from tqdm import tqdm


class ImageListRaw(ImageFolder):
    ATTRIBUTES = {"race": 1, "gender": 2, "age": 3, "recognition": 1}
    MAX_CLASS = {"race": 4, "gender": 1, "age": 100, "recognition": float("Inf")}

    def __init__(self, attribute_name, source, mask_source, image_list, train=True):
        attribute = self.ATTRIBUTES[attribute_name]
        max_class = self.MAX_CLASS[attribute_name]

        image_names = pd.read_csv(image_list, delimiter=" ", header=None)
        image_names = np.asarray(image_names)

        # remove images that have labels outside of desired range
        image_names = image_names[image_names[:, attribute].astype("int") >= 0]
        image_names = image_names[image_names[:, attribute].astype("int") <= max_class]

        if source is not None:
            self.samples = [
                path.join(source, image_name) for image_name in image_names[:, 0]
            ]
            if mask_source is not None:
                self.masks = [
                    path.join(mask_source, image_name)
                    for image_name in image_names[:, 0]
                ]
                self.masks = [
                    f"{image_name[:-4]}_mask.png" for image_name in self.masks
                ]
            else:
                self.masks = None
        else:
            self.samples = image_names[:, 0]

        self.targets = image_names[:, attribute].astype("int")
        if attribute_name == "age":
            self.classnum = np.max(self.targets)
        else:
            self.classnum = np.max(self.targets) + 1

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        with open(self.samples[index], "rb") as f:
            img = f.read()

        if self.masks is not None:
            with open(self.masks[index], "rb") as f:
                mask = f.read()

        return img, self.targets[index], mask


class CustomRawLoader(DataLoader):
    def __init__(self, attribute, workers, source, mask_source, image_list):
        self._dataset = ImageListRaw(attribute, source, mask_source, image_list)

        super(CustomRawLoader, self).__init__(
            self._dataset, num_workers=workers, collate_fn=lambda x: x
        )


def dumps_pyarrow(obj):
    return lz4framed.compress(pa.serialize(obj).to_buffer())


def list2lmdb(
    attribute,
    source,
    mask_source,
    image_list,
    dest,
    num_workers=16,
    write_frequency=5000,
):
    print("Loading dataset from %s" % image_list)
    data_loader = CustomRawLoader(
        attribute, num_workers, source, mask_source, image_list
    )

    name = f"{path.split(image_list)[1][:-4]}.lmdb"
    lmdb_path = path.join(dest, name)
    isdir = path.isdir(lmdb_path)

    print(f"Generate LMDB to {lmdb_path}")

    image_size = 224
    size = len(data_loader.dataset) * image_size * image_size * 3
    if mask_source is not None:
        size *= 2

    print(f"LMDB max size: {size}")

    db = lmdb.open(
        lmdb_path,
        subdir=isdir,
        map_size=size * 2,
        readonly=False,
        meminit=False,
        map_async=True,
    )

    print(len(data_loader.dataset))
    txn = db.begin(write=True)
    for idx, data in tqdm(enumerate(data_loader)):
        # print(type(data), data)
        image, label, mask = data[0]
        if mask is not None:
            txn.put(
                "{}".format(idx).encode("ascii"), dumps_pyarrow((image, label, mask))
            )
        else:
            txn.put("{}".format(idx).encode("ascii"), dumps_pyarrow((image, label)))
        if idx % write_frequency == 0:
            print("[%d/%d]" % (idx, len(data_loader)))
            txn.commit()
            txn = db.begin(write=True)

    # finish iterating through dataset
    txn.commit()
    keys = ["{}".format(k).encode("ascii") for k in range(idx + 1)]
    with db.begin(write=True) as txn:
        txn.put(b"__keys__", dumps_pyarrow(keys))
        txn.put(b"__len__", dumps_pyarrow(len(keys)))
        txn.put(b"__classnum__", dumps_pyarrow(data_loader.dataset.classnum))

    print("Flushing database ...")
    db.sync()
    db.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--image_list", "-l", help="List of images.")
    parser.add_argument("--source", "-s", help="Path to the images.")
    parser.add_argument(
        "--mask_source", "-ms", help="Path to the image masks [optional]."
    )
    parser.add_argument("--workers", "-w", help="Workers number.", default=16, type=int)
    parser.add_argument(
        "--attribute",
        "-a",
        help="Which attribute to load [race, gender, age, recognition].",
        type=str,
    )
    parser.add_argument("--dest", "-d", help="Path to save the lmdb file.")

    args = parser.parse_args()

    list2lmdb(
        args.attribute,
        args.source,
        args.mask_source,
        args.image_list,
        args.dest,
        args.workers,
    )
