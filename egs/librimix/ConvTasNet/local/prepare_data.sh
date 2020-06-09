#!/bin/bash

storage_dir=
python_path=python

. ./utils/parse_options.sh

current_dir=$(pwd)
# Clone LibriMix repo
git clone https://github.com/JorisCos/LibriMix

# Run generation script
cd LibriMix
. generate_librimix.sh $storage_dir

cd $current_dir
$python_path local/create_local_metadata.py --librimix_dir $storage_dir/LibriMix

