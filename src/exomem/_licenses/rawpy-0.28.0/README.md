# Camera raw decoder distribution notices

Exomem's `image-codecs` extra installs `rawpy==0.28.*` on every platform except Intel macOS.
The Python wrapper uses the MIT licence.
The adjacent `LICENSE.txt` and `LICENSE.LibRaw.txt` preserve the upstream wheel notices unchanged.

The released wheels bundle these native libraries:

| Library | Version | Upstream wheel notice | Corresponding source |
| --- | --- | --- | --- |
| LibRaw | 0.22.2 | LGPL-2.1 | https://github.com/LibRaw/LibRaw |
| libgomp (Linux wheels) | 8.5.0 | none in the wheel; GPL-3.0 with the GCC Runtime Library Exception | https://gcc.gnu.org/ |
| libjasper (Linux wheels) | soname 7 | none in the wheel; JasPer License 2.0 | https://github.com/jasper-software/jasper |
| libjpeg (Linux wheels) | soname 8 | none in the wheel; IJG and BSD-3-Clause (libjpeg-turbo) | https://github.com/libjpeg-turbo/libjpeg-turbo |
| liblcms2 (Linux wheels) | soname 2 | none in the wheel; MIT | https://github.com/mm2/Little-CMS |

The wheel records only LibRaw's and libgomp's versions; the other three are named by the shared-object version in `rawpy.libs`, not by release.

LibRaw itself is offered under the LGPL-2.1 or the CDDL-1.0; the wheel ships the LGPL-2.1 text.
Exomem uses AGPL-3.0-or-later.

The Python source and release files are available from https://github.com/letmaik/rawpy and https://pypi.org/project/rawpy/0.28.0/.
