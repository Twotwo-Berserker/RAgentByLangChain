"""
JWT认证工具
提供密码哈希、Token生成、验证以及登录权限装饰器
"""
import hmac
import re
import hashlib
import jwt
from datetime import datetime, timedelta
from functools import wraps
from flask import request, g, current_app
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError


# argon2id 加盐哈希，用的是 argon2-cffi 的默认参数（m=64MiB, t=3, p=4），
# 单次校验约几十毫秒——刻意让离线爆破变贵。哈希自带随机盐，同一明文两次结果不同。
_password_hasher = PasswordHasher()

# 改造前存量密码是无盐MD5（32位小写十六进制）。识别它只为登录时一次性升级成argon2，
# 新写入的密码永远只走 hash_password()。
_LEGACY_MD5_PATTERN = re.compile(r'[0-9a-f]{32}')


def hash_password(password):
    """
    对密码做argon2id加盐哈希
    :param password: 原始密码
    :return: argon2id哈希字符串（约97字符，含算法、参数与随机盐）
    """
    return _password_hasher.hash(password)


def is_legacy_hash(stored):
    """
    判断存储的密码是否为改造前的无盐MD5哈希
    :param stored: 数据库中存储的密码哈希
    :return: 是MD5返回True，否则返回False
    """
    return isinstance(stored, str) and _LEGACY_MD5_PATTERN.fullmatch(stored) is not None


def _legacy_md5_hex(text):
    """
    计算无盐MD5
    仅用于校验存量数据，不得用于新增密码
    :param text: 原始字符串
    :return: MD5加密后的字符串
    """
    return hashlib.md5(text.encode('utf-8')).hexdigest()


def verify_password(password, stored):
    """
    校验密码，兼容存量MD5哈希
    :param password: 用户提交的原始密码
    :param stored: 数据库中存储的密码哈希
    :return: 校验通过返回True，否则返回False
    """
    # 存量MD5走单独分支。用compare_digest而非==，避免按字节提前返回的计时差异
    if is_legacy_hash(stored):
        return hmac.compare_digest(_legacy_md5_hex(password), stored)

    # 脏数据（NULL、空串、被截断的旧哈希）必须返回False而不是抛异常，
    # 否则库里一条坏数据就能把登录接口打成500。
    # 注意 InvalidHashError 继承自 ValueError，与 VerificationError 不同源，两者都要接。
    if not isinstance(stored, str):
        return False
    try:
        return _password_hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):
        return False


def generate_token(user_id, role):
    """
    生成JWT Token
    :param user_id: 用户ID
    :param role: 用户角色
    :return: Token字符串
    """
    payload = {
        'user_id': user_id,
        'role': role,
        'exp': datetime.utcnow() + timedelta(seconds=current_app.config['JWT_EXPIRATION'])
    }
    token = jwt.encode(payload, current_app.config['SECRET_KEY'], algorithm='HS256')
    return token


def verify_token(token):
    """
    验证JWT Token
    :param token: Token字符串
    :return: 解码后的payload或None
    """
    try:
        payload = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def login_required(f):
    """
    登录验证装饰器
    要求请求头中携带有效的Authorization Token
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('Authorization', '')
        if token.startswith('Bearer '):
            token = token[7:]

        if not token:
            from utils.response import error
            return error('请先登录', 401)

        payload = verify_token(token)
        if not payload:
            from utils.response import error
            return error('登录已过期，请重新登录', 401)

        # 将用户信息存入g对象，供后续使用
        g.user_id = payload['user_id']
        g.role = payload['role']
        return f(*args, **kwargs)

    return decorated


def admin_required(f):
    """
    管理员权限装饰器
    要求用户已登录且角色为admin
    """
    @wraps(f)
    @login_required
    def decorated(*args, **kwargs):
        if g.role != 'admin':
            from utils.response import error
            return error('权限不足，需要管理员权限', 403)
        return f(*args, **kwargs)

    return decorated
