# doubao-skills-hub

Doubao Agent 技能（Skill）集合，每个子目录是一个可独立安装的技能。

## 目录

| 技能 | 说明 |
|---|---|
| [sspanel-purchase](skills/sspanel-purchase/) | SSPanel-UIM 面板登录、充值出码、后台监控到账、自动购买套餐并取回订阅链接 |

## 安装

1. 把对应技能整个文件夹复制到目标环境的用户技能目录 `.user_skills/` 下，保证 `SKILL.md` 位于该技能目录的第一层。
2. 安装依赖（按需）：

   ```bash
   pip install requests pillow
   ```

3. 技能不含账号密码与登录态，使用时在目标环境重新 `login` 即可。

## 说明

- 会话与凭证存放在运行环境的 `~/.cache/sspanel-purchase/`，不随仓库提交。
- 更换面板站点时，通过 `--base` / `--gateway` 或环境变量 `SSPANEL_BASE` / `SSPANEL_GATEWAY` 指定。
