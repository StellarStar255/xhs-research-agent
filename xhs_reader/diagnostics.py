"""Small, user-requested diagnostic reports without chats, logs, or credentials."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import sys
from urllib.parse import urlsplit, urlunsplit

from . import __version__, paths, settings


def redact(text, secrets=()):
    text = str(text)
    for secret in sorted(set(s for s in secrets if isinstance(s, str) and s), key=len, reverse=True):
        text = text.replace(secret, '[已隐藏]') if len(secret) >= 4 else re.sub(r'(?<!\w)' + re.escape(secret) + r'(?!\w)', '[已隐藏]', text)
    text = re.sub(r'''(?im)(["']?authorization["']?\s*[:=]\s*)[^\r\n,;]+''', r'\1[已隐藏]', text)
    text = re.sub(r'''(?im)(["']?(?:cookie|set-cookie)["']?\s*[:=]\s*)[^\r\n]+''', r'\1[已隐藏]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [已隐藏]', text)
    text = re.sub(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|cookie|set-cookie|x-api-key)["']?\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;<>]+)''', r'\1[已隐藏]', text)
    text = re.sub(r'\b(?:sk-|ghp_|gho_|github_pat_)[A-Za-z0-9_-]{12,}\b', '[已隐藏]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[已隐藏]', text)
    def url(match):
        try:
            parsed = urlsplit(match.group())
            host = parsed.netloc.rsplit('@', 1)[-1]
            return urlunsplit((parsed.scheme, host, parsed.path, '[参数已隐藏]' if parsed.query else '', ''))
        except ValueError:
            return '[地址已隐藏]'
    text = re.sub(r'''https?://[^\s<>"']+''', url, text)
    # Request paths can carry phone access keys even when no absolute URL is present.
    text = re.sub(r'([?&][^\s=&#]+)=([^\s&#]*)', r'\1=[已隐藏]', text)
    return text.replace(str(Path.home()), '~')


def report(operation='', error=''):
    conf = settings.load()
    secrets = [conf.get('api', {}).get('api_key', ''), conf.get('mobile', {}).get('token', ''),
               *conf.get('api_credentials', {}).values()]
    secrets += [value for name, value in os.environ.items() if re.search(r'(?:API_KEY|TOKEN|PASSWORD|SECRET)', name, re.I)]
    backend = conf.get('backend', '')
    model = conf.get('api', {}).get('model', '') if backend == 'api' else conf.get(backend + '_model', '')
    data = {'应用版本': __version__, '时间': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            '系统': platform.system() + ' ' + platform.release(), '架构': platform.machine(),
            '运行方式': '安装版' if paths.FROZEN else '源码', 'Python': sys.version.split()[0],
            '回答后端': backend, '模型': model or '默认', '操作': operation[:200], '错误': error[:6000] or '尚未记录错误'}
    return json.dumps({key: redact(value, secrets) for key, value in data.items()}, ensure_ascii=False, indent=2)
