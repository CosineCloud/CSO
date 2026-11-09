"""Lossless codecs with bounded input chunks and safe pause checkpoints."""
import bz2
import io
import lzma
import zipfile

MODES=('off','zip','bz2','xz','auto')
CHUNK=1024*1024


def compress_content(content,mode,step=None):
    if mode not in MODES:
        raise ValueError('Unsupported compression mode.')
    best,codec=content,'none'
    choices=('zip','bz2','xz') if mode=='auto' else (() if mode=='off' else (mode,))
    for selected in choices:
        if step:step(f'Compressing with {selected.upper()} at maximum strength.')
        output=io.BytesIO()
        if selected=='zip':
            with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
                with archive.open('payload','w') as target:
                    for offset in range(0,len(content),CHUNK):
                        if step:step(None)
                        target.write(content[offset:offset+CHUNK])
        else:
            compressor=bz2.BZ2Compressor(9) if selected=='bz2' else lzma.LZMACompressor(format=lzma.FORMAT_XZ,preset=9|lzma.PRESET_EXTREME)
            for offset in range(0,len(content),CHUNK):
                if step:step(None)
                output.write(compressor.compress(content[offset:offset+CHUNK]))
            if step:step(None)
            output.write(compressor.flush())
        result=output.getvalue()
        if len(result)<len(best):
            best,codec=result,selected
    if step:
        step('Compression complete: '+(codec.upper()+' selected.' if codec!='none' else 'original is smallest; compression skipped.'))
    return best,codec


def decompress_content(content,codec,size):
    if codec=='none':
        return content
    if codec=='zip':
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries=archive.infolist()
            if len(entries)!=1 or entries[0].filename!='payload' or entries[0].file_size!=size:
                raise ValueError('Invalid ZIP metadata.')
            with archive.open(entries[0]) as source:
                result=source.read(size+1)
    elif codec in ('bz2','xz'):
        decoder=bz2.BZ2Decompressor() if codec=='bz2' else lzma.LZMADecompressor(format=lzma.FORMAT_XZ,memlimit=256*1024*1024)
        result=decoder.decompress(content,max_length=size+1)
        if not decoder.eof or decoder.unused_data:
            raise ValueError('Invalid compressed stream or size.')
    else:
        raise ValueError('Unknown compressed format.')
    if len(result)!=size:
        raise ValueError('Decompressed size mismatch.')
    return result
