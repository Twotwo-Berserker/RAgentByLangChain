"""
认证路由
提供用户登录和获取用户信息接口
"""
from flask import Blueprint, request, g, current_app
from models import db
from models.user import User
from utils.auth import hash_password, is_legacy_hash, verify_password, generate_token, login_required
from utils.response import success, error

# 创建认证蓝图
auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['POST'])
def login():
    """
    用户登录接口
    请求参数: username, password
    返回: token和用户信息
    """
    data = request.get_json()
    if not data:
        return error('请提供登录信息')

    username = data.get('username', '').strip()
    password = data.get('password', '').strip()

    if not username or not password:
        return error('用户名和密码不能为空')

    # 查找用户
    user = User.query.filter_by(username=username).first()
    if not user:
        return error('用户名或密码错误')

    # 验证密码（argon2id 哈希比对，兼容存量的无盐MD5）
    if not verify_password(password, user.password):
        return error('用户名或密码错误')

    # 检查用户状态
    if user.status != 1:
        return error('账号已被禁用，请联系管理员')

    # 存量MD5密码：本次凭据已验证通过，就地升级为argon2id哈希，用户无感。
    # 升级失败不应影响本次登录（密码已经验对了），记日志即可，下次登录会再试一次。
    if is_legacy_hash(user.password):
        try:
            user.password = hash_password(password)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.warning(f'用户{user.username}的密码哈希升级失败（不影响本次登录）: {e}')

    # 生成Token
    token = generate_token(user.id, user.role)

    return success({
        'token': token,
        'user': user.to_dict()
    }, '登录成功')


@auth_bp.route('/info', methods=['GET'])
@login_required
def get_user_info():
    """
    获取当前登录用户信息
    需要携带有效Token
    """
    user = User.query.get(g.user_id)
    if not user:
        return error('用户不存在', 404)

    return success(user.to_dict())
