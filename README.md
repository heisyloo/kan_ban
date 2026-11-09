# 持仓看板

个人持仓看板静态页面，托管于 GitHub Pages。

- 访问地址：https://heisyloo.github.io/kan-ban/
- 单文件 `index.html`，无构建步骤，无外部 JS/CSS 依赖。
- 价格由浏览器在打开页面时从 Stooq 实时拉取，每 5 分钟自动刷新；拉取失败或缺失时显示上次价格并给出提示。
- 总资产与利润走势按月登记，数据保存在浏览器 localStorage（`kanban-monthly`），仅保留近 12 个月。

部署：仓库 Settings → Pages → Build and deployment → Deploy from a branch → 选择 `main` 分支、根目录（`/`）保存。
