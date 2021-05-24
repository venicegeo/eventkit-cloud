#!/usr/bin/env bash

set -e

./configure --prefix=${PREFIX}

make
make install

