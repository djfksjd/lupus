"""Local live slash picker. Owns one input region, never the terminal's alternate screen."""
from __future__ import annotations
import bisect
from dataclasses import dataclass
import os
import re
import select
import sys
import termios
import time
import tty
import unicodedata
from collections.abc import Callable

MAX_INPUT = 65_536
MAX_CANDIDATES = 512

class InputCancelled(KeyboardInterrupt):
    """Synthetic input cancellation, distinct from the user pressing Ctrl+C."""

class InputTooLong(InputCancelled):
    """Cancel an over-limit line; never submit a shortened request."""

ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')

@dataclass(frozen=True)
class Candidate:
    value: str
    label: str = ''
    description: str = ''
    kind: str = 'command'
    action: str = 'insert'  # insert an argument, expand a submenu, or submit a safe explicit choice


def available() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty() and os.isatty(sys.stdin.fileno())
                    and os.isatty(sys.stdout.fileno()) and os.environ.get('TERM','') not in ('','dumb'))
    except (OSError,ValueError,AttributeError):
        return False


def _plain(text) -> str:
    return ' '.join(''.join(c for c in str(text) if c.isprintable()).split())


def _display(text: str) -> str:
    return text.replace('\r','␍').replace('\n','↵').replace('\t','    ')


def _cells(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1
               for c in ANSI.sub('',text))


def _clip(text: str, width: int) -> str:
    if width<=0:return ''
    out=[];used=0
    for c in text:
        step=0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1
        if used+step>width:break
        out.append(c);used+=step
    return ''.join(out)


class _Reader:
    def __init__(self,fd):self.fd=fd;self.pending=bytearray();self.utf8=bytearray()
    def get(self,timeout=.1):
        if not self.pending:
            if not select.select([self.fd],[],[],max(0,timeout))[0]:return None
            chunk=os.read(self.fd,1)
            if not chunk:raise EOFError
            self.pending.extend(chunk)
        value=self.pending[0];del self.pending[0];return value
    def key(self):
        first=self.utf8[0] if self.utf8 else self.get()
        if first is None:return None
        if first==27:
            second=self.get(.04)
            if second is None:return 'escape'
            if second not in (ord('['),ord('O')):
                self.pending.insert(0,second)
                return 'escape'
            sequence=bytes([second])
            for _ in range(30):
                value=self.get(.04)
                if value is None:return 'escape'
                if value==3:return 'interrupt'
                if value==4:return 'eof'
                sequence+=bytes([value])
                if 64<=value<=126:break
            keys={b'[A':'up',b'[B':'down',b'[C':'right',b'[D':'left',b'[H':'home',b'[F':'end',
                  b'OH':'home',b'OF':'end',b'[1~':'home',b'[4~':'end',b'[3~':'delete',b'[200~':'paste'}
            return keys.get(sequence,'ignore')
        controls={3:'interrupt',4:'eof',1:'home',5:'end',21:'kill_before',11:'kill_after',23:'kill_word',
                  8:'backspace',127:'backspace',9:'tab',10:'enter',13:'enter',12:'redraw'}
        if first in controls:return controls[first]
        if first<32:return 'ignore'
        if first<128:return ('text',chr(first))
        if not self.utf8:
            if not 0xC2<=first<=0xF4:return ('text','\ufffd')
            self.utf8.append(first)
        count=2 if first<0xE0 else 3 if first<0xF0 else 4
        while len(self.utf8)<count:
            value=self.get(.1)
            if value is None:return None
            if not 0x80<=value<=0xBF:
                # The next key belongs to its own parser branch, especially controls.
                self.pending.insert(0,value)
                raw=bytes(self.utf8);self.utf8.clear()
                return ('text',raw.decode('utf-8',errors='replace'))
            self.utf8.append(value)
        raw=bytes(self.utf8);self.utf8.clear()
        return ('text',raw.decode('utf-8',errors='replace'))
    def paste(self):
        raw=bytearray();deadline=time.monotonic()+10;end=b'\x1b[201~'
        while time.monotonic()<deadline:
            value=self.get(min(.1,max(0,deadline-time.monotonic())))
            if value is None:continue
            if value==3:raise KeyboardInterrupt
            if value==4:raise EOFError
            raw.append(value)
            if raw.endswith(end):
                value=raw[:-len(end)].decode('utf-8',errors='replace')
                return ''.join(c for c in value if c.isprintable() or c in '\r\n\t')
            if len(raw)>MAX_INPUT*4+len(end):raise InputTooLong
        # Incomplete paste is cancelled, never submitted as a command or an exit signal.
        raise InputCancelled


class _Region:
    def __init__(self,stream,prompt,paint):self.stream=stream;self.prompt=prompt;self.paint=paint;self.height=0
    def size(self):
        try:return os.get_terminal_size(self.stream.fileno())
        except OSError:return os.terminal_size((80,24))
    def clear(self):
        if not self.height:return
        out='\r'+(f'\x1b[{self.height-1}A' if self.height>1 else '')
        for index in range(self.height):
            out+='\x1b[2K'
            if index+1<self.height:out+='\x1b[1B\r'
        if self.height>1:out+=f'\x1b[{self.height-1}A'
        self.stream.write(out+'\r')
    def draw(self,text,cursor,choices,selected):
        columns,rows=self.size();width=max(2,columns-1);lines=[]
        visible=max(1,min(7,rows-3))
        offset=max(0,selected-visible+1)
        for index,candidate in enumerate(choices[offset:offset+visible],offset):
            label=_plain(candidate.label or candidate.value);description=_plain(candidate.description)
            name_width=min(max(12,width//3),max(2,width-4))
            name=_clip(label,name_width)
            line=('› ' if index==selected else '  ')+name
            if description:
                line+=' ' * max(1,name_width-_cells(name)+2)+description
            line=_clip(line,width)
            if index==selected:
                if self.paint.on:line='\x1b[7m'+line+' '*(width-_cells(line))+'\x1b[27m'
                line=self.paint.accent(line,bold=True)
            else:line=self.paint.faint(line)
            lines.append(line)
        if choices:
            lines.append(self.paint.faint(_clip('↑↓ select · Enter choose · Tab fill · Esc close',width)))
        prefix=_clip(ANSI.sub('',self.prompt),max(1,width//3))
        room=max(1,width-_cells(prefix));widths=[0]
        for c in text:widths.append(widths[-1]+_cells(_display(c)))
        left=bisect.bisect_left(widths,max(0,widths[cursor]-room+1))
        left=min(left,cursor)
        shown=_clip(_display(text[left:]),room)
        painted=self.paint.accent(prefix,bold=True)+shown
        lines.append(painted)
        self.clear();self.stream.write('\r\n'.join(lines))
        column=min(width,_cells(prefix)+widths[cursor]-widths[left])
        self.stream.write('\r'+(f'\x1b[{column}C' if column else ''))
        self.stream.flush();self.height=len(lines)
    def finish(self,text):
        self.draw(text,len(text),[],0)
        self.stream.write('\r\n');self.stream.flush();self.height=0


def read_line(prompt: str, suggest: Callable[[str,int],list[Candidate]], paint, history: list[str] | None = None) -> str:
    """Enter follows a candidate's explicit action; Tab fills without executing.

    Only call on a supported TTY. All terminal ownership ends before returning or raising.
    Suggestions are local metadata; callback errors leave the editable buffer intact.
    """
    fd=sys.stdin.fileno();saved=termios.tcgetattr(fd);reader=_Reader(fd);region=_Region(sys.stdout,prompt,paint)
    text='';cursor=0;choices=[];selected=0;suppressed=None;history=[''.join(c for c in item if c.isprintable() or c in '\r\n\t')[:MAX_INPUT] for item in list(history or [])[-500:] if isinstance(item,str)];hpos=len(history);draft=''
    last_query=None;last_render=None;cancellation_note=None
    try:
        tty.setraw(fd,when=termios.TCSANOW)
        sys.stdout.write('\x1b[?2004h');sys.stdout.flush()
        while True:
            if (text,cursor)!=last_query:
                selected=0;last_query=(text,cursor)
                try:
                    choices=list(suggest(text,cursor))[:MAX_CANDIDATES] if text.startswith('/') and suppressed!=text else []
                    choices=[item for item in choices if isinstance(item,Candidate) and item.value and len(item.value)<=1024
                             and all(c.isprintable() and not c.isspace() for c in item.value)]
                except Exception:choices=[]
            size=region.size()
            signature=(text,cursor,tuple(choices),selected,size)
            if signature!=last_render:
                region.draw(text,cursor,choices,selected);last_render=signature
            key=reader.key()
            if key is None:continue
            if key=='redraw':last_render=None;continue
            if key=='interrupt':raise KeyboardInterrupt
            if key=='eof':
                if not text:raise EOFError
                text=text[:cursor]+text[cursor+1:];continue
            if key=='escape':choices=[];suppressed=text;continue
            if key in ('enter','tab') and choices:
                choice=choices[selected]
                value=choice.value
                start=cursor
                while start>0 and not text[start-1].isspace():start-=1
                end=cursor
                while end<len(text) and not text[end].isspace():end+=1
                suffix='' if choice.action=='expand' and value.endswith('/') else ' '
                replacement=text[:start]+value+suffix+text[end:].lstrip(' ')
                if len(replacement)>MAX_INPUT:
                    raise InputTooLong
                text=replacement;cursor=start+len(value)+len(suffix)
                choices=[]
                if choice.action=='expand':
                    suppressed=None;last_query=None
                elif key=='enter' and choice.action=='submit':
                    return text
                else:suppressed=text
                continue
            if key=='tab':
                suppressed=None;last_query=None
                continue
            if key=='enter':return text
            if key in ('up','down'):
                step=-1 if key=='up' else 1
                if choices:selected=(selected+step)%len(choices)
                elif history:
                    if hpos==len(history):draft=text
                    hpos=max(0,min(len(history),hpos+step))
                    text=history[hpos] if hpos<len(history) else draft;cursor=len(text)
                continue
            if key=='left':cursor=max(0,cursor-1)
            elif key=='right':cursor=min(len(text),cursor+1)
            elif key=='home':cursor=0
            elif key=='end':cursor=len(text)
            elif key=='backspace' and cursor:text=text[:cursor-1]+text[cursor:];cursor-=1
            elif key=='delete':text=text[:cursor]+text[cursor+1:]
            elif key=='kill_before':text=text[cursor:];cursor=0
            elif key=='kill_after':text=text[:cursor]
            elif key=='kill_word':
                left=cursor
                while left and text[left-1].isspace():left-=1
                while left and not text[left-1].isspace():left-=1
                text=text[:left]+text[cursor:];cursor=left
            elif key=='paste' or isinstance(key,tuple):
                value=reader.paste() if key=='paste' else key[1]
                value=''.join(c for c in value if c.isprintable() or (key=='paste' and c in '\r\n\t'))
                if len(text)+len(value)>MAX_INPUT:raise InputTooLong
                text=text[:cursor]+value+text[cursor:];cursor+=len(value)
    except InputCancelled as exc:
        cancellation_note = f'Input exceeds {MAX_INPUT} characters; line cancelled.' if isinstance(exc,InputTooLong) else 'Incomplete paste; line cancelled.'
        raise
    finally:
        try:
            region.finish(text)
        finally:
            try:sys.stdout.write('\x1b[?2004l');sys.stdout.flush()
            finally:termios.tcsetattr(fd,termios.TCSADRAIN,saved)
        if cancellation_note:
            sys.stdout.write(cancellation_note+'\n');sys.stdout.flush()
