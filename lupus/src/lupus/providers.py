"""Public provider configuration. Credentials are references to environment variables, never values."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import http.client
import time
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import urllib.parse
from .util import LupusError, find_secret


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    protocol: str
    base_url: str = ''
    api_key_env: str | None = None
    default_model: str | None = None
    models: tuple[str, ...] = ()

    @property
    def driver(self):
        if self.protocol == 'native': return 'native_' + self.id
        digest = hashlib.sha256((self.base_url+'\n'+str(self.api_key_env)).encode()).hexdigest()[:16]
        return 'api:' + self.id + ':' + digest

    @property
    def provider(self):
        return {'codex':'openai','claude':'anthropic'}.get(self.id, self.driver)

    def public(self):
        native = self.protocol == 'native'
        available = bool(shutil.which(self.id)) if native else bool(self.base_url) and (not self.api_key_env or bool(os.environ.get(self.api_key_env)))
        return {'id':self.id,'name':self.name,'protocol':self.protocol,'base_url':self.base_url,
                'api_key_env':self.api_key_env,'default_model':self.default_model,'models':list(self.models),
                'driver':self.driver,'provider':self.provider,'available':available,
                'availability_reason':'executable present' if native and available else 'executable missing' if native else
                    'endpoint configuration required' if not self.base_url else 'environment reference present; not authenticated' if self.api_key_env and available else
                    'environment reference missing' if self.api_key_env else 'configuration present; server not contacted',
                'capabilities':{'tools':'inherited native CLI' if native else 'LUPUS workspace tools',
                                'capability_status':'declared, not a provider compatibility measurement'},
                'effective_backend_confirmed':False}


def validate_model(model):
    if not isinstance(model,str) or find_secret(model) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,199}',model):
        raise LupusError('MODEL_INVALID','use a nonempty model identifier without spaces or credentials')
    return model


def validate_url(value):
    if not isinstance(value,str) or any(ord(c)<33 for c in value) or find_secret(value): raise LupusError('PROVIDER_CONFIG_INVALID','base_url must be a URL without control characters')
    p=urllib.parse.urlsplit(value)
    if p.username or p.password or p.query or p.fragment or not p.hostname:
        raise LupusError('PROVIDER_CONFIG_INVALID','base_url cannot contain credentials, query or fragment')
    local=p.hostname.lower()=='localhost'
    try: local=local or ipaddress.ip_address(p.hostname).is_loopback
    except ValueError: pass
    if p.scheme!='https' and not (p.scheme=='http' and local):
        raise LupusError('PROVIDER_CONFIG_INVALID','use HTTPS or an explicit loopback HTTP endpoint')
    return value.rstrip('/')


def _specs(home=None):
    specs={'codex':ProviderSpec('codex','Codex','native'), 'claude':ProviderSpec('claude','Claude Code','native'),
           'glm':ProviderSpec('glm','GLM · Z.AI','openai-chat','https://api.z.ai/api/paas/v4','ZAI_API_KEY'),
           'ollama':ProviderSpec('ollama','Ollama','openai-chat','http://127.0.0.1:11434/v1'),
           'openrouter':ProviderSpec('openrouter','OpenRouter','openai-chat','https://openrouter.ai/api/v1','OPENROUTER_API_KEY'),
           'deepseek':ProviderSpec('deepseek','DeepSeek','openai-chat','https://api.deepseek.com','DEEPSEEK_API_KEY'),
           'gemini':ProviderSpec('gemini','Gemini · OpenAI protocol','openai-chat','https://generativelanguage.googleapis.com/v1beta/openai','GEMINI_API_KEY'),
           'lmstudio':ProviderSpec('lmstudio','LM Studio','openai-chat','http://localhost:1234/v1'),
           'openai-compatible':ProviderSpec('openai-compatible','OpenAI-compatible','openai-chat')}
    path=Path(home or os.environ.get('LUPUS_HOME') or Path.home()/'.lupus')/'providers.json'
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_size>262144:
            raise LupusError('PROVIDER_CONFIG_INVALID','providers.json must be a small regular public configuration file')
        try:
            data=json.loads(path.read_text()); entries=data.get('providers',[]) if isinstance(data,dict) else data
            if not isinstance(entries,list) or len(entries)>64: raise ValueError()
            for row in entries:
                if not isinstance(row,dict) or set(row)-{'id','name','protocol','base_url','api_key_env','default_model','models'}: raise ValueError()
                ident=row['id']
                if not isinstance(ident,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}',ident) or ident in ('codex','claude'): raise ValueError()
                if row.get('protocol','openai-chat')!='openai-chat': raise ValueError()
                key=row.get('api_key_env')
                if key is not None and (not isinstance(key,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]*(?:KEY|TOKEN)',key)): raise ValueError()
                model=row.get('default_model'); models=row.get('models',[])
                if model is not None:validate_model(model)
                if not isinstance(models,list) or len(models)>256:raise ValueError()
                for m in models:validate_model(m)
                name=row.get('name',ident)
                if not isinstance(name,str) or any(ord(c)<32 for c in name) or find_secret(name):raise ValueError()
                specs[ident]=ProviderSpec(ident,name[:80],'openai-chat',validate_url(row['base_url']),key,model,tuple(models))
        except (ValueError,TypeError,KeyError,UnicodeError):
            raise LupusError('PROVIDER_CONFIG_INVALID','invalid public provider profile; store only api_key_env references, never keys') from None
    return specs


def catalog(home=None):return [spec.public() for spec in _specs(home).values()]


def resolve(driver, home=None):
    specs=_specs(home)
    if driver in ('native_codex','native_claude'):driver=driver[7:]
    if isinstance(driver,str) and driver.startswith('api:'):
        found=next((spec for spec in specs.values() if spec.driver==driver),None)
    else:found=specs.get(driver)
    if found is None:raise LupusError('DRIVER_UNKNOWN','unknown provider; use lupus providers')
    return found


def request_json(url, *, key=None, payload=None, timeout=20, max_bytes=4*1024*1024):
    """No proxies/redirects/retries: a key is sent only to the explicit approved endpoint."""
    headers={'Content-Type':'application/json'}
    if key:headers['Authorization']='Bearer '+key
    parsed=urllib.parse.urlsplit(url)
    connection=(http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection)(parsed.hostname,parsed.port,timeout=timeout)
    deadline=time.monotonic()+timeout
    response=None
    try:
        connection.request('GET' if payload is None else 'POST',parsed.path or '/',body=None if payload is None else json.dumps(payload).encode(),headers=headers)
        sock=connection.sock
        if sock:sock.settimeout(max(.001,deadline-time.monotonic()))
        response=connection.getresponse()
        if not 200<=response.status<300:
            code='quota' if response.status in (402,429) else 'auth' if response.status in (401,403) else 'api_error'
            raise LupusError(code,'provider HTTP status '+str(response.status))
        chunks=[];size=0
        while True:
            if response.isclosed() or response.length == 0: break
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError()
            if sock:sock.settimeout(remaining)
            piece=response.read1(min(65536,max_bytes-size+1))
            if not piece:break
            size+=len(piece)
            if size>max_bytes:raise LupusError('API_RESPONSE_TOO_LARGE','provider response exceeded bounded capture')
            chunks.append(piece)
        value=json.loads(b''.join(chunks).decode('utf-8'))
        if not isinstance(value,dict):raise ValueError()
        return value
    except (http.client.HTTPException,TimeoutError,OSError):
        raise LupusError('api_unavailable','provider request failed or timed out') from None
    except (ValueError,UnicodeError):raise LupusError('API_RESPONSE_INVALID','provider returned invalid JSON') from None
    finally:
        if response:response.close()
        connection.close()


def models(driver,home=None,*,discover=False):
    spec=resolve(driver,home);items=list(dict.fromkeys((*spec.models,*([spec.default_model] if spec.default_model else []))))
    if discover:
        if spec.protocol=='native':raise LupusError('MODEL_DISCOVERY_UNSUPPORTED','native model discovery is not invoked automatically')
        if not spec.base_url:raise LupusError('PROVIDER_CONFIG_REQUIRED','configure this provider endpoint first')
        key=os.environ.get(spec.api_key_env) if spec.api_key_env else None
        if spec.api_key_env and not key:raise LupusError('API_AUTH_MISSING','set the configured API key environment variable')
        value=request_json(spec.base_url+'/models',key=key,timeout=5,max_bytes=1024*1024)
        items=[validate_model(row['id']) for row in value.get('data',[]) if isinstance(row,dict) and 'id' in row][:256]
    return {'provider':spec.id,'models':items,'source':'explicit discovery' if discover else 'public configuration',
            'available':spec.public()['available'],'effective_backend_confirmed':False}
