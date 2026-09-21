"""
Flask应用入口
创建Flask实例，注册蓝图，初始化数据库和CORS
"""
import os
from flask import Flask
from flask_cors import CORS
from config import Config
from models import db


def create_app():
    """
    应用工厂函数
    创建并配置Flask应用实例
    :return: Flask应用实例
    """
    app = Flask(__name__)
    app.config.from_object(Config)

    # 初始化数据库
    db.init_app(app)

    # 启用跨域支持
    CORS(app, supports_credentials=True)

    # 确保上传目录存在
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

    # 注册蓝图（路由模块）
    from routes.auth import auth_bp
    from routes.knowledge_base import kb_bp
    from routes.document import doc_bp
    from routes.chat import chat_bp
    from routes.user import user_bp
    from routes.stats import stats_bp

    app.register_blueprint(auth_bp, url_prefix='/api/auth')
    app.register_blueprint(kb_bp, url_prefix='/api/knowledge_base')
    app.register_blueprint(doc_bp, url_prefix='/api/document')
    app.register_blueprint(chat_bp, url_prefix='/api/chat')
    app.register_blueprint(user_bp, url_prefix='/api/user')
    app.register_blueprint(stats_bp, url_prefix='/api/stats')

    # 恢复上次进程退出时卡在 uploading 的文档（延迟导入，避免顶层引入服务层）
    # 恢复失败不应阻断服务启动：数据库暂时不可达时，服务照常起来并提供（能提供的）接口，
    # 否则会因为一次数据库抖动而完全无法启动，问题反而更难定位
    try:
        from services.task_queue import recover_pending_documents
        recover_pending_documents(app)
    except Exception as e:
        app.logger.warning(f'启动恢复未完成任务失败（不影响服务启动）: {e}')

    return app


if __name__ == '__main__':
    app = create_app()
    # use_reloader=False 是必须的：reloader 会再 fork 一个进程，
    # 导致线程池、VectorService 单例、L1 缓存各存在两份，并且两个进程同时写同一个
    # chroma_data/chroma.sqlite3。注意不能用 app.debug 来判断——debug 是 app.run() 才施加的。
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
