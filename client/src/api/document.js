/**
 * 文档管理API
 */
import request from './index'

/** 获取文档列表（分页） */
export function getDocList(params) {
  return request.get('/document/list', { params })
}

/** 上传文档（立即返回，向量化在后台进行） */
export function uploadDoc(data) {
  return request.post('/document/upload', data, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000
  })
}

/** 查询单个文档的处理状态与向量化进度 */
export function getDocStatus(id) {
  return request.get(`/document/${id}`)
}

/** 删除文档 */
export function deleteDoc(id) {
  return request.delete(`/document/${id}`)
}
