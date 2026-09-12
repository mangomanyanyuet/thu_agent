/**
 * 摄像头 / 麦克风可用性诊断。
 *
 * 背景：浏览器只在「安全上下文」里暴露 navigator.mediaDevices。
 * 安全上下文 = https://* 或 http://localhost / http://127.0.0.1。
 * 用 http://192.168.x.x 或 http://<内网IP> 访问时，navigator.mediaDevices 是
 * **undefined** —— 此时不是"用户拒绝授权"，而是浏览器根本不给这个 API，
 * 授权弹窗永远不会出现。若不做诊断，界面上只会表现为"按钮灰着/点了没反应"，
 * 用户无法区分是权限问题、设备问题还是协议问题。
 */

const LOCALHOST = new Set(['localhost', '127.0.0.1', '[::1]', '::1'])

/**
 * @returns {{ok: boolean, reason: string, fix: string}}
 *   ok     —— 摄像头/麦克风 API 可用
 *   reason —— 不可用的原因（人话）
 *   fix    —— 可执行的解决建议
 */
export function mediaDiagnosis() {
  if (typeof window === 'undefined') {
    return { ok: false, reason: '非浏览器环境', fix: '' }
  }

  if (!window.isSecureContext) {
    const host = window.location.hostname
    if (LOCALHOST.has(host)) {
      return {
        ok: false,
        reason: '当前页面未被判定为安全上下文',
        fix: '换用 Chrome / Edge 最新版，或改用 https 访问',
      }
    }
    return {
      ok: false,
      reason: `当前用 http://${host} 访问，浏览器禁用摄像头与麦克风`,
      fix:
        '两种办法：① 在本机用 http://localhost:8000 打开；' +
        '② 用 https 访问（后端加自签证书，手机需先信任）。' +
        '微信小程序不受此限制（走 wx.getRecorderManager / <camera> 原生 API）。',
    }
  }

  if (!navigator.mediaDevices?.getUserMedia) {
    return {
      ok: false,
      reason: '当前浏览器不支持 getUserMedia',
      fix: '换用 Chrome / Edge / Safari 最新版',
    }
  }

  if (!window.MediaRecorder) {
    return {
      ok: false,
      reason: '当前浏览器不支持 MediaRecorder，无法录音',
      fix: '换用 Chrome / Edge / Firefox；Safari 需 14.1+',
    }
  }

  return { ok: true, reason: '', fix: '' }
}

/** 把 getUserMedia 的异常翻译成人话 */
export function explainMediaError(e, kind = '麦克风') {
  const name = e?.name || ''
  const map = {
    NotAllowedError: `你（或浏览器设置）拒绝了${kind}权限，请在地址栏的权限图标里重新允许`,
    PermissionDeniedError: `你（或浏览器设置）拒绝了${kind}权限，请在地址栏的权限图标里重新允许`,
    NotFoundError: `没有找到可用的${kind}设备`,
    DevicesNotFoundError: `没有找到可用的${kind}设备`,
    NotReadableError: `${kind}被其他程序占用（如会议软件），请关闭后重试`,
    TrackStartError: `${kind}被其他程序占用（如会议软件），请关闭后重试`,
    OverconstrainedError: `${kind}不支持请求的参数，请换设备重试`,
    SecurityError: `浏览器出于安全策略阻止了${kind}（通常是因为非 https 访问）`,
  }
  return map[name] || `${kind}打开失败：${e?.message || e}`
}
