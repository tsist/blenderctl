# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded Unicode cmap reader for SFNT TTF/OTF and TTC face zero. No dependencies."""
import struct,hashlib,unicodedata
from protocol import Failure

def inspect_bytes(data,text):
    if len(data)>134217728:raise Failure('UNSUPPORTED','Font exceeds 128 MiB')
    def need(offset,size):
        if offset<0 or size<0 or offset+size>len(data):raise Failure('INVALID_REQUEST','Truncated or invalid font offset')
    def u16(p):need(p,2);return struct.unpack_from('>H',data,p)[0]
    def u32(p):need(p,4);return struct.unpack_from('>I',data,p)[0]
    need(0,12);base=0;faces=1
    if data[:4]==b'ttcf':
        faces=u32(8)
        if not 1<=faces<=256:raise Failure('UNSUPPORTED','Invalid TTC face count')
        need(12,faces*4);base=u32(12)
    need(base,12)
    if data[base:base+4] not in (b'\x00\x01\x00\x00',b'OTTO',b'true'):raise Failure('UNSUPPORTED','Only SFNT TTF/OTF or TTC face zero is supported')
    count=u16(base+4)
    if not 1<=count<=256:raise Failure('INVALID_REQUEST','Invalid SFNT table count')
    need(base+12,count*16);tables={}
    for i in range(count):
        p=base+12+16*i;tag=data[p:p+4];offset=u32(p+8);size=u32(p+12);need(offset,size)
        if tag in tables:raise Failure('INVALID_REQUEST','Duplicate SFNT table')
        tables[tag]=(offset,size)
    if b'cmap' not in tables or b'maxp' not in tables:raise Failure('UNSUPPORTED','Font needs Unicode cmap and maxp')
    if b'fvar' in tables or b'CFF2' in tables:raise Failure('UNSUPPORTED','Variable fonts require another adapter')
    if not {b'glyf',b'loca'}.issubset(tables):raise Failure('UNSUPPORTED','This adapter requires static TrueType outlines')
    maxp,size=tables[b'maxp']
    if size<6:raise Failure('INVALID_REQUEST','Invalid maxp')
    glyph_count=u16(maxp+4);start,size=tables[b'cmap'];end=start+size
    if not glyph_count:raise Failure('INVALID_REQUEST','Font has no glyphs')
    if size<4:raise Failure('INVALID_REQUEST','Invalid cmap')
    n=u16(start+2)
    if n>256 or 4+n*8>size:raise Failure('INVALID_REQUEST','Invalid cmap encoding records')
    candidates=[]
    for i in range(n):
        p=start+4+i*8;platform=u16(p);encoding=u16(p+2);offset=start+u32(p+4)
        if not(platform==0 or platform==3 and encoding in (1,10)):continue
        if offset<start or offset+2>end:raise Failure('INVALID_REQUEST','Invalid cmap subtable offset')
        fmt=u16(offset)
        if fmt not in (4,12):continue
        if offset+(4 if fmt==4 else 8)>end:raise Failure('INVALID_REQUEST','Truncated cmap header')
        length=u16(offset+2) if fmt==4 else u32(offset+4)
        if offset+length>end:raise Failure('INVALID_REQUEST','Truncated cmap subtable')
        if fmt==12:
            if length<16:raise Failure('INVALID_REQUEST','Short cmap12')
            groups=u32(offset+12)
            if groups>200000 or 16+12*groups>length:raise Failure('INVALID_REQUEST','Invalid cmap12 groups')
            spans=[];previous=-1
            for j in range(groups):
                p=offset+16+12*j;a=u32(p);b=u32(p+4);g=u32(p+8)
                if a>b or a<=previous or b>0x10ffff:raise Failure('INVALID_REQUEST','Invalid Unicode cmap12 range')
                spans.append((a,b,g));previous=b
            candidates.append((fmt,offset,length,spans))
        else:
            if length<16:raise Failure('INVALID_REQUEST','Short cmap4')
            twice=u16(offset+6);segments=twice//2
            if twice%2 or segments<1 or 16+segments*8>length:raise Failure('INVALID_REQUEST','Invalid cmap4 segments')
            ends=offset+14;starts=ends+2*segments+2;previous=-1
            for j in range(segments):
                a=u16(starts+2*j);b=u16(ends+2*j)
                if a>b or a<=previous:raise Failure('INVALID_REQUEST','Invalid or overlapping cmap4 range')
                previous=b
            if previous!=65535:raise Failure('INVALID_REQUEST','Missing cmap4 sentinel')
            candidates.append((fmt,offset,length,segments))
    if not candidates:raise Failure('UNSUPPORTED','Font has no supported Unicode cmap4/cmap12')
    # Prefer full Unicode repertoire, not a union that could conceal conflicting subtables.
    candidates.sort(key=lambda r:-r[0]);fmt,offset,length,records=candidates[0]
    def glyph(cp):
        if fmt==12:
            for a,b,g in records:
                if a<=cp<=b:return g+cp-a
                if a>cp:break
            return 0
        if cp>65535:return 0
        n=records;ends=offset+14;starts=ends+2*n+2;deltas=starts+2*n;ranges=deltas+2*n
        for i in range(n):
            a=u16(starts+2*i);b=u16(ends+2*i)
            if a>b:raise Failure('INVALID_REQUEST','Invalid cmap4 range')
            if a<=cp<=b:
                delta=u16(deltas+2*i);r=u16(ranges+2*i)
                if r==0:return (cp+delta)&65535
                p=ranges+2*i+r+2*(cp-a)
                if p<offset or p+2>offset+length:raise Failure('INVALID_REQUEST','Invalid cmap4 glyph offset')
                g=u16(p);return (g+delta)&65535 if g else 0
        return 0
    rows=[];unsupported=[]
    for cp in sorted(set(map(ord,text))):
        ch=chr(cp)
        if ch in '\n\r\t':continue
        # Standalone glyph layout only; do not promise shaping or variation sequences.
        if unicodedata.category(ch).startswith(('M','C')) or unicodedata.bidirectional(ch) in ('R','AL','AN'):
            unsupported.append('U+%04X'%cp)
        g=glyph(cp);rows.append({'codepoint':'U+%04X'%cp,'glyph_id':g,'present':0<g<glyph_count})
    return {'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data),'face_index':0,'collection_faces':faces,
        'cmap_format':fmt,'outline_format':'static_truetype','glyph_count':glyph_count,'characters':rows,'missing':[r['codepoint'] for r in rows if not r['present']],
        'unsupported_shaping':unsupported,'coverage':'standalone_unicode_cmap; no shaping/variation or visual glyph-quality guarantee'}
