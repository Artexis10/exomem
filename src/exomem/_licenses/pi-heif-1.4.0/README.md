# Image codec distribution notices

Exomem's `image-codecs` extra installs `pi-heif==1.4.0` and Pillow 11.1.0 or later.
The Python wrapper uses BSD-3-Clause.
The adjacent `LICENSE.txt` and `LICENSES_bundled.txt` preserve the upstream wheel notices unchanged.

The manylinux x86-64 wheel for CPython 3.14 bundles these native libraries.
The wheel carries a decoder only: it has no encoder and no x265.

| Library | Version | Upstream wheel notice | Corresponding source |
| --- | --- | --- | --- |
| libheif | 1.23.0 | LGPLv3 | https://github.com/strukturag/libheif/tree/v1.23.0 |
| libde265 | 1.1.0 | LGPLv3 | https://github.com/strukturag/libde265/tree/v1.1.0 |

The versions above are what the wheel reports at run time through `pi_heif.libheif_info()`.
The version numbers in the upstream `LICENSES_bundled.txt` are older than the libraries in the wheel.
Both libraries are LGPLv3 in either case.
These licence facts do not establish patent clearance.

The Python source and release files are available from https://github.com/bigcat88/pillow_heif and https://pypi.org/project/pi-heif/1.4.0/.
