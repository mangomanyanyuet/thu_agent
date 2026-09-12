// 全局：用户标识、服务端地址、热线缓存
App({
  globalData: {
    // Demo 用「游客 + 本机存储」的方式造一个稳定的 user_id。
    // 生产环境应换成微信登录后的服务端 userId（openid 映射），
    // 且必须做数据最小化与可删除（PRD §8）。
    userId: '',
    apiBase: '',
    bootstrap: null,
  },

  onLaunch() {
    let uid = wx.getStorageSync('userId')
    if (!uid) {
      uid = 'mini_' + Math.random().toString(36).slice(2, 10)
      wx.setStorageSync('userId', uid)
    }
    this.globalData.userId = uid

    if (!wx.getStorageSync('apiBase')) {
      wx.setStorageSync('apiBase', 'http://127.0.0.1:8000')
    }
    this.globalData.apiBase = wx.getStorageSync('apiBase')
  },

  setApiBase(url) {
    const base = (url || '').trim().replace(/\/+$/, '')
    wx.setStorageSync('apiBase', base)
    this.globalData.apiBase = base
    this.globalData.bootstrap = null
  },
})
