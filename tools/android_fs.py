#!/usr/bin/env python3
"""Read files out of Android images through byte-range reads only
(Python 3.9+, standard library). Used by pixel_gsc_fetch.py to reach
/vendor/firmware inside a factory image without downloading it:

    super.img (Android sparse) -> LP metadata -> vendor partition extents
    -> ext4 or EROFS directory walk -> the file's blocks

Every layer is a `RangeSource` (anything with `.len` and `.read(start, end)`),
so the same code runs over a local file, an HTTP URL or a ZIP member.
EROFS files compressed with LZ4 are decompressed here; LZMA, DEFLATE and
ZSTD-compressed EROFS files are reported, not read.
"""
import struct


class Error(Exception):
    pass


class Range:
    """Base: a fixed-length blob readable by byte range."""
    len = 0

    def read(self, start, end):
        raise NotImplementedError

    def u(self, fmt, off):
        return struct.unpack("<" + fmt, self.read(off, off + struct.calcsize("<" + fmt)))


class Bytes(Range):
    def __init__(self, data):
        self.data, self.len = data, len(data)

    def read(self, start, end):
        return self.data[start:end]


class Extents(Range):
    """A blob made of (logical_start, length, source, source_offset) pieces;
    gaps read as zeros, and a piece whose source is None reads as `fill`."""
    def __init__(self, pieces, length):
        self.pieces = sorted(pieces)
        self.len = length

    def read(self, start, end):
        out, pos = [], start
        for lstart, length, src, soff, fill in self.pieces:
            lend = lstart + length
            if lend <= pos:
                continue
            if lstart >= end:
                break
            if lstart > pos:
                out.append(b"\0" * (lstart - pos))
                pos = lstart
            take_end = min(end, lend)
            if src is None:
                n = take_end - pos
                out.append((fill * (n // len(fill) + 1))[:n])
            else:
                out.append(src.read(soff + (pos - lstart), soff + (take_end - lstart)))
            pos = take_end
            if pos >= end:
                break
        if pos < end:
            out.append(b"\0" * (end - pos))
        return b"".join(out)


# ------------------------------------------------------------- sparse image

SPARSE_MAGIC = 0xED26FF3A


def is_sparse(src):
    return src.len >= 28 and src.u("I", 0)[0] == SPARSE_MAGIC


def unsparse(src):
    """The flat image behind an Android sparse image, as a Range. Reads
    one chunk header per chunk (a few hundred small reads for super.img)."""
    magic, major, minor, fhs, chs, blk, total_blks, total_chunks, _ = src.u("IHHHHIIII", 0)
    if magic != SPARSE_MAGIC:
        raise Error("not a sparse image")
    pieces, pos, lpos = [], fhs, 0
    for _ in range(total_chunks):
        ctype, _, csz, tsz = src.u("HHII", pos)
        nbytes = csz * blk
        if ctype == 0xCAC1:  # raw
            pieces.append((lpos, nbytes, src, pos + chs, None))
        elif ctype == 0xCAC2:  # fill
            pieces.append((lpos, nbytes, None, 0, src.read(pos + chs, pos + chs + 4)))
        elif ctype in (0xCAC3, 0xCAC4):  # don't care / crc
            pass
        else:
            raise Error(f"sparse chunk type {ctype:#x} unknown")
        if ctype != 0xCAC4:
            lpos += nbytes
        pos += tsz
    return Extents(pieces, total_blks * blk)


def flat(src):
    return unsparse(src) if is_sparse(src) else src


# ------------------------------------------------------ LP (super) metadata

LP_GEOMETRY_MAGIC = 0x616C4467
LP_METADATA_MAGIC = 0x414C5030


def lp_partitions(src, slot=0):
    """{name: Range} for the logical partitions in a flat super image."""
    if src.u("I", 4096)[0] != LP_GEOMETRY_MAGIC:
        raise Error("no LP geometry at 4096: not a super image")
    max_size, slot_count = src.u("II", 4096 + 40)
    base = 4096 + 2 * 4096 + slot * max_size
    magic, major, minor, header_size = src.u("IHHI", base)
    if magic != LP_METADATA_MAGIC:
        raise Error("bad LP metadata header")
    p_off, p_num, p_size, e_off, e_num, e_size = src.u("IIIIII", base + 80)
    tables = base + header_size
    extents = []
    for i in range(e_num):
        num_sectors, ttype, tdata, tsrc = src.u("QIQI", tables + e_off + i * e_size)
        extents.append((num_sectors * 512, ttype, tdata * 512))
    parts = {}
    for i in range(p_num):
        e = tables + p_off + i * p_size
        name = src.read(e, e + 36).split(b"\0", 1)[0].decode("ascii", "replace")
        _, first, count, _ = src.u("IIII", e + 36)
        pieces, lpos = [], 0
        for length, ttype, off in extents[first:first + count]:
            if ttype == 0:
                pieces.append((lpos, length, src, off, None))
            lpos += length
        parts[name] = Extents(pieces, lpos)
    return parts


# -------------------------------------------------------------------- ext4

class Ext4:
    MAGIC = 0xEF53

    def __init__(self, src):
        self.src = src
        sb = src.read(1024, 2048)
        if struct.unpack("<H", sb[56:58])[0] != self.MAGIC:
            raise Error("not ext4")
        (self.inodes_count, self.blocks_lo) = struct.unpack("<II", sb[0:8])
        self.first_data_block, log_bs = struct.unpack("<II", sb[20:28])
        self.bs = 1024 << log_bs
        self.ipg = struct.unpack("<I", sb[40:44])[0]
        self.inode_size = struct.unpack("<H", sb[88:90])[0] or 128
        incompat = struct.unpack("<I", sb[96:100])[0]
        self.desc_size = struct.unpack("<H", sb[254:256])[0] if incompat & 0x80 else 32
        if self.desc_size == 0:
            self.desc_size = 32
        self.gdt = (self.first_data_block + 1) * self.bs

    def inode(self, ino):
        group, index = divmod(ino - 1, self.ipg)
        d = self.gdt + group * self.desc_size
        table = struct.unpack("<I", self.src.read(d + 8, d + 12))[0]
        if self.desc_size >= 64:
            table |= struct.unpack("<I", self.src.read(d + 40, d + 44))[0] << 32
        off = table * self.bs + index * self.inode_size
        raw = self.src.read(off, off + 160)
        mode, size_lo = struct.unpack("<HxxI", raw[0:8])
        flags = struct.unpack("<I", raw[32:36])[0]
        size = size_lo | (struct.unpack("<I", raw[108:112])[0] << 32)
        return {"mode": mode, "size": size, "flags": flags, "i_block": raw[40:100]}

    def _extents(self, node, out):
        magic, entries, _, depth = struct.unpack("<HHHH", node[0:8])
        if magic != 0xF30A:
            raise Error("bad extent header")
        for i in range(entries):
            e = node[12 + i * 12:24 + i * 12]
            if depth == 0:
                lblk, length, hi, lo = struct.unpack("<IHHI", e)
                if length > 32768:
                    length -= 32768  # uninitialised extent; read it anyway
                out.append((lblk * self.bs, length * self.bs, self.src, (lo | hi << 32) * self.bs, None))
            else:
                _, leaf_lo, leaf_hi = struct.unpack("<IIH2x", e)
                blk = (leaf_lo | leaf_hi << 32) * self.bs
                self._extents(self.src.read(blk, blk + self.bs), out)

    def data(self, inode):
        """A file's contents as a Range."""
        if inode["flags"] & 0x10000000:
            return Bytes(inode["i_block"][:inode["size"]])  # inline data
        pieces = []
        if inode["flags"] & 0x80000:
            self._extents(inode["i_block"], pieces)
        else:
            blocks = list(struct.unpack("<15I", inode["i_block"]))
            direct = blocks[:12]
            per = self.bs // 4
            if blocks[12]:
                direct += list(struct.unpack(f"<{per}I", self.src.read(blocks[12] * self.bs, (blocks[12] + 1) * self.bs)))
            if blocks[13]:
                for ind in struct.unpack(f"<{per}I", self.src.read(blocks[13] * self.bs, (blocks[13] + 1) * self.bs)):
                    if ind:
                        direct += list(struct.unpack(f"<{per}I", self.src.read(ind * self.bs, (ind + 1) * self.bs)))
            for i, b in enumerate(direct):
                if b:
                    pieces.append((i * self.bs, self.bs, self.src, b * self.bs, None))
        return Extents(pieces, inode["size"])

    def listdir(self, inode):
        """[(name, ino, is_dir)] of a directory inode."""
        data = self.data(inode)
        raw = data.read(0, data.len)
        out, pos = [], 0
        while pos + 8 <= len(raw):
            ino, rec_len, name_len, ftype = struct.unpack("<IHBB", raw[pos:pos + 8])
            if rec_len < 8:
                break
            name = raw[pos + 8:pos + 8 + name_len].decode("utf-8", "replace")
            if ino and name not in (".", ".."):
                out.append((name, ino, ftype == 2))
            pos += rec_len
        return out

    def walk(self, ino=2, prefix=""):
        """Every regular file as (path, size, reader) where reader() returns bytes."""
        for name, child, is_dir in self.listdir(self.inode(ino)):
            path = f"{prefix}/{name}"
            if is_dir:
                yield from self.walk(child, path)
            else:
                node = self.inode(child)
                if node["mode"] & 0xF000 == 0x8000:
                    yield path, node["size"], (lambda n=node: self.data(n).read(0, n["size"]))


# ------------------------------------------------------------------- erofs

EROFS_MAGIC = 0xE0F5E1E2


def lz4_block(src, out_len):
    """Decompress one raw LZ4 block (no frame) to exactly out_len bytes."""
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i:i + lit]
        i += lit
        if i >= n or len(out) >= out_len:
            break
        off = src[i] | src[i + 1] << 8
        i += 2
        mlen = token & 15
        if mlen == 15:
            while True:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - off
        if off == 0 or start < 0:
            raise Error("corrupt LZ4 block")
        if off >= mlen:
            out += out[start:start + mlen]
        else:
            for _ in range(mlen):
                out.append(out[start])
                start += 1
        if len(out) >= out_len:  # the block may be zero-padded after the stream
            break
    if len(out) != out_len:
        raise Error(f"LZ4 block decoded to {len(out)} bytes, expected {out_len}")
    return bytes(out)


class Erofs:
    NONHEAD, HEAD1, PLAIN, HEAD2 = 2, 1, 0, 3
    D0_CBLKCNT = 1 << 11

    def __init__(self, src):
        self.src = src
        sb = src.read(1024, 1024 + 128)
        if struct.unpack("<I", sb[0:4])[0] != EROFS_MAGIC:
            raise Error("not erofs")
        self.blkszbits = sb[12]
        self.bs = 1 << self.blkszbits
        self.root_nid = struct.unpack("<H", sb[14:16])[0]
        self.meta_blkaddr = struct.unpack("<I", sb[40:44])[0]
        self.feature_incompat = struct.unpack("<I", sb[80:84])[0]
        self.packed_nid = struct.unpack("<Q", sb[96:104])[0]

    def inode(self, nid):
        off = self.meta_blkaddr * self.bs + nid * 32
        raw = self.src.read(off, off + 64)
        fmt = struct.unpack("<H", raw[0:2])[0]
        extended = fmt & 1
        layout = (fmt >> 1) & 7
        xattr_icount = struct.unpack("<H", raw[2:4])[0]
        mode = struct.unpack("<H", raw[4:6])[0]
        if extended:
            size = struct.unpack("<Q", raw[8:16])[0]
            iu = struct.unpack("<I", raw[16:20])[0]
            isize = 64
        else:
            size = struct.unpack("<I", raw[8:12])[0]
            iu = struct.unpack("<I", raw[16:20])[0]
            isize = 32
        xattr_size = 12 + (xattr_icount - 1) * 4 if xattr_icount else 0
        return {"nid": nid, "off": off, "layout": layout, "mode": mode, "size": size,
                "u": iu, "end": off + isize + xattr_size}

    # -- plain / inline / chunk layouts
    def data(self, node):
        size, layout = node["size"], node["layout"]
        if layout == 0:  # flat plain
            return Extents([(0, size, self.src, node["u"] * self.bs, None)], size)
        if layout == 2:  # flat inline: full blocks at raw_blkaddr, tail after the inode
            tail = size % self.bs
            head = size - tail
            pieces = []
            if head:
                pieces.append((0, head, self.src, node["u"] * self.bs, None))
            if tail:
                pieces.append((head, tail, self.src, node["end"], None))
            return Extents(pieces, size)
        if layout == 4:  # chunk based
            fmt = node["u"]
            chunkbits = self.blkszbits + (fmt & 0x1F)
            indexes = fmt & 0x20
            csize = 1 << chunkbits
            count = (size + csize - 1) // csize
            esize = 8 if indexes else 4
            raw = self.src.read(node["end"], node["end"] + count * esize)
            pieces = []
            for i in range(count):
                blk = struct.unpack("<I", raw[i * esize + (4 if indexes else 0):i * esize + esize])[0]
                if blk != 0xFFFFFFFF:
                    pieces.append((i * csize, min(csize, size - i * csize), self.src, blk * self.bs, None))
            return Extents(pieces, size)
        if layout in (1, 3):
            return Bytes(self.decompress(node))
        raise Error(f"erofs data layout {layout} unknown")

    # -- compressed layouts (z_erofs)
    def _zhead(self, node):
        pos = (node["end"] + 7) & ~7
        raw = self.src.read(pos, pos + 8)
        fragmentoff = struct.unpack("<I", raw[0:4])[0]
        idata_size = struct.unpack("<H", raw[2:4])[0]
        advise = struct.unpack("<H", raw[4:6])[0]
        algo, cbits = raw[6], raw[7]
        # Legacy full indexes sit after a further 8 bytes of header padding.
        ebase = pos + 8 + (8 if node["layout"] == 1 else 0)
        return {"pos": pos, "ebase": ebase, "advise": advise, "algo": algo,
                "lclusterbits": self.blkszbits + (cbits & 7), "fragmentoff": fragmentoff,
                "idata_size": idata_size, "legacy": node["layout"] == 1,
                "whole_fragment": bool(cbits & 0x80)}

    def _lcluster(self, z, lcn, totalidx):
        """{type, clusterofs, delta0, cblks, pblk, nextpackoff} for one logical cluster."""
        if lcn >= totalidx:
            return None
        if z["legacy"]:
            pos = z["ebase"] + lcn * 8
            advise, clusterofs, u = struct.unpack("<HHI", self.src.read(pos, pos + 8))
            t = advise & 3
            m = {"type": t, "nextpackoff": pos + 8, "cblks": None, "delta0": 0, "pblk": None, "clusterofs": 0}
            if t == self.NONHEAD:
                d0, d1 = u & 0xFFFF, u >> 16
                m["clusterofs"] = 1 << z["lclusterbits"]
                if d0 & self.D0_CBLKCNT:
                    m["cblks"] = d0 & ~self.D0_CBLKCNT
                    m["delta0"] = 1
                else:
                    m["delta0"] = d0
            else:
                m["clusterofs"], m["pblk"] = clusterofs, u
            return m
        return self._compact_lcluster(z, lcn, totalidx)

    def _compact_lcluster(self, z, lcn, totalidx):
        lclusterbits = z["lclusterbits"]
        if lclusterbits > 14:
            raise Error("erofs logical cluster too big for compact indexes")
        ebase = z["ebase"]
        initial4 = (32 - ebase % 32) // 4
        if initial4 == 8:
            initial4 = 0
        if (z["advise"] & 1) and initial4 < totalidx:
            compacted2 = (totalidx - initial4) // 16 * 16
        else:
            compacted2 = 0
        pos = ebase
        if lcn < initial4:
            shift = 2
        else:
            pos += initial4 * 4
            lcn -= initial4
            if lcn < compacted2:
                shift = 1
            else:
                pos += compacted2 * 2
                lcn -= compacted2
                shift = 2
        pos += lcn << shift
        if (1 << shift) == 4 and lclusterbits <= 14:
            vcnt = 2
        elif (1 << shift) == 2 and lclusterbits <= 12:
            vcnt = 16
        else:
            raise Error("unsupported compact index layout")
        packsize = vcnt << shift
        packstart = pos - pos % packsize
        pack = self.src.read(packstart, packstart + packsize)
        big = bool(z["advise"] & 2)
        lobits = max(lclusterbits, 12)
        encodebits = (packsize - 4) * 8 // vcnt
        i = (pos - packstart) >> shift

        def decode(idx):
            bitpos = encodebits * idx
            v = int.from_bytes(pack[bitpos // 8:bitpos // 8 + 8], "little") >> (bitpos & 7)
            return v & ((1 << lobits) - 1), (v >> lobits) & 3

        lo, t = decode(i)
        m = {"type": t, "nextpackoff": packstart + packsize, "cblks": None, "delta0": 0,
             "pblk": None, "clusterofs": 0}
        if t == self.NONHEAD:
            m["clusterofs"] = 1 << lclusterbits
            if lo & self.D0_CBLKCNT:
                if not big:
                    raise Error("erofs: CBLKCNT without big pcluster")
                m["cblks"] = lo & ~self.D0_CBLKCNT
                m["delta0"] = 1
            elif i + 1 != vcnt:
                m["delta0"] = lo
            else:
                lo2, t2 = decode(i - 1)
                if t2 != self.NONHEAD:
                    lo2 = 0
                elif lo2 & self.D0_CBLKCNT:
                    lo2 = 1
                m["delta0"] = lo2 + 1
            return m
        m["clusterofs"] = lo
        if not big:
            nblk = 1
            while i > 0:
                i -= 1
                lo, t2 = decode(i)
                if t2 == self.NONHEAD:
                    i -= lo
                if i >= 0:
                    nblk += 1
        else:
            nblk = 0
            while i > 0:
                i -= 1
                lo, t2 = decode(i)
                if t2 == self.NONHEAD:
                    if lo & self.D0_CBLKCNT:
                        i -= 1
                        nblk += lo & ~self.D0_CBLKCNT
                        continue
                    if lo <= 1:
                        raise Error("erofs: corrupt compact index")
                    i -= lo - 2
                    continue
                nblk += 1
        m["pblk"] = struct.unpack("<I", pack[packsize - 4:packsize])[0] + nblk
        return m

    def decompress(self, node):
        z = self._zhead(node)
        size = node["size"]
        if size == 0:
            return b""
        lbits = z["lclusterbits"]
        lsize = 1 << lbits
        totalidx = (size + self.bs - 1) // self.bs
        algos = {0: "lz4", 1: "lzma", 2: "deflate", 3: "zstd"}
        inline = bool(z["advise"] & 8)
        interlaced = bool(z["advise"] & 16)
        fragment = bool(z["advise"] & 32)
        if z["whole_fragment"]:  # the whole file lives in the packed inode
            packed = self.inode(self.packed_nid)
            return self.data(packed).read(z["fragmentoff"], z["fragmentoff"] + size)
        # Head of the extent holding the last byte, for the inline/fragment
        # tail; the inline data sits right after the index pack of that
        # last logical cluster.
        tail_lcn = (size - 1) >> lbits
        tail_m = self._lcluster(z, tail_lcn, totalidx)
        idataoff = tail_m["nextpackoff"]
        if tail_m["type"] != self.NONHEAD and ((size - 1) & (lsize - 1)) < tail_m["clusterofs"]:
            tail_lcn -= 1
            tail_m = self._lcluster(z, tail_lcn, totalidx)
        while tail_m["type"] == self.NONHEAD:
            tail_lcn -= tail_m["delta0"]
            tail_m = self._lcluster(z, tail_lcn, totalidx)
        out = bytearray()
        lcn = 0
        while lcn < totalidx:
            m = self._lcluster(z, lcn, totalidx)
            if m["type"] == self.NONHEAD:
                raise Error("erofs: extent does not start with a head cluster")
            start = lcn * lsize + m["clusterofs"]
            if start >= size:
                break
            if start != len(out):
                raise Error(f"erofs: extent gap at {len(out)} vs {start}")
            # Find the next head: the extent ends where it starts.
            nxt, end = lcn + 1, None
            while nxt < totalidx:
                n = self._lcluster(z, nxt, totalidx)
                if n["type"] != self.NONHEAD:
                    end = nxt * lsize + n["clusterofs"]
                    break
                nxt += 1
            if end is None:
                end = size
            length = end - start
            # Physical cluster size: the following NONHEAD may carry the block count.
            cblks = 1
            if lcn + 1 < totalidx:
                n1 = self._lcluster(z, lcn + 1, totalidx)
                if n1["type"] == self.NONHEAD and n1["cblks"]:
                    cblks = n1["cblks"]
            is_tail = lcn == tail_lcn
            if is_tail and fragment:
                packed = self.inode(self.packed_nid)
                out += self.data(packed).read(z["fragmentoff"], z["fragmentoff"] + length)
            else:
                if is_tail and inline:
                    raw = self.src.read(idataoff, idataoff + z["idata_size"])
                else:
                    pa = m["pblk"] * self.bs
                    raw = self.src.read(pa, pa + cblks * self.bs)
                if m["type"] == self.PLAIN:
                    if interlaced:
                        o = m["clusterofs"]
                        piece = (raw[o:] + raw[:o])[:length]
                    else:
                        piece = raw[:length]
                    if len(piece) != length:
                        raise Error("erofs: short plain pcluster")
                    out += piece
                else:
                    algo = z["algo"] & 0xF if m["type"] == self.HEAD1 else z["algo"] >> 4
                    if algo != 0:
                        raise Error(f"erofs: {algos.get(algo, algo)}-compressed file; only LZ4 is supported")
                    if self.feature_incompat & 1:  # 0padding: data is right-aligned
                        raw = raw.lstrip(b"\0")
                    out += lz4_block(raw, length)
            lcn = nxt
        return bytes(out[:size])

    def listdir(self, node):
        data = self.data(node)
        raw = data.read(0, data.len)
        out = []
        for b in range(0, len(raw), self.bs):
            blk = raw[b:b + self.bs]
            if len(blk) < 12:
                break
            count = struct.unpack("<H", blk[8:10])[0] // 12
            offs = [struct.unpack("<H", blk[i * 12 + 8:i * 12 + 10])[0] for i in range(count)] + [len(blk)]
            for i in range(count):
                nid = struct.unpack("<Q", blk[i * 12:i * 12 + 8])[0]
                ftype = blk[i * 12 + 10]
                name = blk[offs[i]:offs[i + 1]].split(b"\0", 1)[0].decode("utf-8", "replace")
                if name not in (".", ".."):
                    out.append((name, nid, ftype == 2))
        return out

    def walk(self, nid=None, prefix=""):
        node = self.inode(self.root_nid if nid is None else nid)
        for name, child, is_dir in self.listdir(node):
            path = f"{prefix}/{name}"
            if is_dir:
                yield from self.walk(child, path)
            else:
                c = self.inode(child)
                if c["mode"] & 0xF000 == 0x8000:
                    yield path, c["size"], (lambda n=c: self.data(n).read(0, n["size"]))


# --------------------------------------------------------------- front door

def open_fs(src):
    """An Ext4 or Erofs reader over a flat or sparse filesystem image."""
    src = flat(src)
    if src.len >= 1024 + 128:
        if struct.unpack("<I", src.read(1024, 1028))[0] == EROFS_MAGIC:
            return Erofs(src)
        if struct.unpack("<H", src.read(1024 + 56, 1024 + 58))[0] == Ext4.MAGIC:
            return Ext4(src)
    raise Error("neither ext4 nor erofs")


def vendor_from_super(src, names=("vendor_a", "vendor")):
    """The vendor partition Range inside a (sparse or flat) super image."""
    parts = lp_partitions(flat(src))
    for n in names:
        if n in parts and parts[n].len:
            return parts[n], n
    raise Error(f"no vendor partition in super (have {', '.join(sorted(parts))})")


def find_files(src, want, root_hint=None):
    """(path, size, reader) for every file in a vendor/super image whose path
    satisfies `want(path)`. Directories are pruned to `root_hint` (e.g.
    '/firmware') when given, so a whole image is not walked."""
    fs = open_fs(src)
    for path, size, reader in _walk_under(fs, root_hint):
        if want(path):
            yield path, size, reader


def _walk_under(fs, root_hint):
    if not root_hint:
        yield from fs.walk()
        return
    parts = [p for p in root_hint.split("/") if p]
    if isinstance(fs, Ext4):
        ino = 2
        for p in parts:
            hit = [c for n, c, d in fs.listdir(fs.inode(ino)) if n == p and d]
            if not hit:
                return
            ino = hit[0]
        yield from fs.walk(ino, "/" + "/".join(parts))
    else:
        nid = fs.root_nid
        for p in parts:
            hit = [c for n, c, d in fs.listdir(fs.inode(nid)) if n == p and d]
            if not hit:
                return
            nid = hit[0]
        yield from fs.walk(nid, "/" + "/".join(parts))
