# Image codec distribution notices

Exomem's `image-codecs` extra installs `pillow-heif==1.8.1` and Pillow 11.1.0 or later.
The Python wrapper uses BSD-3-Clause.
The adjacent `LICENSE.txt` and `LICENSES_bundled.txt` preserve the upstream wheel notices unchanged.

The released CPython 3.12 manylinux x86-64 wheel bundles these native libraries:

| Library | Version | Upstream wheel notice | Corresponding source |
| --- | --- | --- | --- |
| libheif | 1.23.6 | LGPLv3 | https://github.com/strukturag/libheif/tree/v1.23.6 |
| libde265 | 1.1.3 | LGPLv3 | https://github.com/strukturag/libde265/tree/v1.1.3 |
| x265 | 4.3 | GPLv2 | https://github.com/Multicorewareinc/x265/tree/4.3 |

The x265 4.3 `source/x265.h` header permits GPL version 2 or any later version.
The wheel notice labels that library GPLv2; both statements are retained here.
Exomem uses AGPL-3.0-or-later.
Windows wheels also carry the MinGW runtime notices listed in `LICENSES_bundled.txt`.
These licence facts do not establish patent clearance.

The Python source and release files are available from https://github.com/bigcat88/pillow_heif and https://pypi.org/project/pillow-heif/1.8.1/.
