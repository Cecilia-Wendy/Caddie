# Caddie 网申采集器

本地 Chrome Manifest V3 扩展。读取当前可见且已有值的表单字段，经预览确认后保存到本机 `127.0.0.1:8766` 的 Caddie。

安装：打开 `chrome://extensions`，启用开发者模式，选择“加载已解压的扩展程序”，然后选择本目录。

安全边界：不自动提交；不读取文件内容；排除密码、验证码、身份证、银行卡及隐藏字段；仅获得用户点击扩展时的当前页面权限。

字段识别思路参考 AutoPhil（MIT）：https://github.com/SonwaneyY/autophil
