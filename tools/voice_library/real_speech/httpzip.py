# -*- coding: utf-8 -*-
"""Read-only random access into a remote ZIP via HTTP Range requests.

Used for VCTK (CC BY 4.0): the official archive is ~11.7 GB, so we never download it -
we read the End Of Central Directory + central directory, then fetch only the individual
member entries we need.  Requires the server to answer Range requests (probed, and every
read is checked for a real 206 + matching Content-Range via Http.get_range).
"""
import io
import zipfile

PREFETCH = 256 * 1024


class RangeReader(io.RawIOBase):
    def __init__(self, http, url, size):
        self.http, self.url, self.size = http, url, size
        self.pos = 0
        self.buf = b""
        self.buf_start = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, offset, whence=0):
        if whence == 1:
            offset = self.pos + offset
        elif whence == 2:
            offset = self.size + offset
        self.pos = max(0, min(int(offset), self.size))
        return self.pos

    def tell(self):
        return self.pos

    def _fill(self, start, want):
        end = min(start + max(want, PREFETCH), self.size) - 1
        data, err = self.http.get_range(self.url, start, end)
        if data is None:
            raise IOError("range read failed @%d: %s" % (start, err))
        self.buf, self.buf_start = data, start

    def readinto(self, b):
        need = len(b)
        if need == 0 or self.pos >= self.size:
            return 0
        if not (self.buf_start <= self.pos < self.buf_start + len(self.buf)):
            self._fill(self.pos, need)
        out = b""
        while len(out) < need and self.pos < self.size:
            off = self.pos - self.buf_start
            if off >= len(self.buf):
                self._fill(self.pos, need - len(out))
                off = 0
                if not self.buf:
                    break
            take = min(need - len(out), len(self.buf) - off)
            out += self.buf[off:off + take]
            self.pos += take
        b[:len(out)] = out
        return len(out)


def open_remote_zip(http, url):
    """-> (ZipFile, total_size).  Raises IOError when Range is unsupported."""
    st, cr, cl = http.head_range(url, 64)
    if st != 206 or not cr or "/" not in str(cr):
        raise IOError("range unsupported status=%s content_range=%s" % (st, cr))
    size = int(str(cr).rsplit("/", 1)[1])
    zf = zipfile.ZipFile(RangeReader(http, url, size))
    return zf, size
