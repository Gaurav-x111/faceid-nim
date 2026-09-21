#!/bin/sh
set -e
echo "== daemon =="
sudo systemctl stop faceid-nimd
sudo cp daemon/target/release/faceid-nimd /usr/libexec/faceid-nim/faceid-nimd
echo "== worker (staged install: in-tree build/ is root-owned) =="
sudo systemctl stop faceid-vision.service
stage=$(mktemp -d /tmp/faceid-stage.XXXXXX)
cp -r vision/faceid_vision "$stage/"
cp vision/pyproject.toml "$stage/"
sudo /usr/libexec/faceid-nim/venv/bin/python3 -m pip install \
    --force-reinstall --no-deps "$stage"
rm -rf "$stage"
echo "== restart both =="
sudo systemctl start faceid-nimd faceid-vision.service
sleep 1
sudo systemctl status faceid-nimd faceid-vision.service --no-pager | grep Active
