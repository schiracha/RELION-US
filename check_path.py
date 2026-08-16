#!/usr/bin/env python3
import os
import shutil

print("Python environment check:")
print(f"PATH (first 300 chars): {os.environ.get('PATH')[:300]}")
print()
print("shutil.which results:")
print(f"  relion_refine: {shutil.which('relion_refine')}")
print(f"  python3: {shutil.which('python3')}")
print(f"  which: {shutil.which('which')}")
