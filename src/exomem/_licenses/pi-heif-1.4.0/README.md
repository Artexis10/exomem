# Image codec distribution notices

Exomem's `image-codecs` extra installs `pi-heif==1.4.0` and Pillow 11.1.0 or later.
The Python wrapper uses BSD-3-Clause.
The adjacent `LICENSE.txt` and `LICENSES_bundled.txt` preserve the upstream wheel notices unchanged.

The images ship the manylinux wheels for CPython 3.12 on x86-64 and aarch64, which bundle these native libraries.
The wheels carry an HEVC decoder only: they have no HEVC or AV1 encoder and no x265.

| Library | Version | Upstream wheel notice | Corresponding source |
| --- | --- | --- | --- |
| libheif | 1.23.0 | LGPLv3 | https://github.com/strukturag/libheif/tree/v1.23.0 |
| libde265 | 1.1.0 | LGPLv3 | https://github.com/strukturag/libde265/tree/v1.1.0 |

The versions above are what the x86-64 wheel reports at run time through `pi_heif.libheif_info()`.
The aarch64 wheel bundles the same library files, `libheif.so.1.23.0` and `libde265.so.0.2.0`; it was not run.
The version numbers in the upstream `LICENSES_bundled.txt` are older than the libraries in the wheel.
Both libraries are LGPLv3 in either case.
These licence facts do not establish patent clearance.

The Python source and release files are available from https://github.com/bigcat88/pillow_heif and https://pypi.org/project/pi-heif/1.4.0/.
