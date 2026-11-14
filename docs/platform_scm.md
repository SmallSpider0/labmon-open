# SCM 平台适配器协议说明

本文说明可选 SCM 适配器使用的接口形状；地址、账号、资源池、路径与响应内容均为虚构示例，不含生产请求记录或第三方前端源码。不同部署版本须先对照平台文档核验；普通 SSH worker 不依赖此适配器。实现位于 `labmon/platform.py`、`labmon/fleetops.py` 与 `labmon/renew.py`。

## 通用

- 前端用 axios，`baseURL` 是 `/api`。登录后每个请求在 `Authorization` 头里直接放令牌（不加 `Bearer`）。
- 返回统一是 `{"code": "0"|"-1"|"-2", "msg": …, "data": …}`。`"0"` 成功；`"-2"` 令牌失效，前端会跳回登录页。
- 令牌是 JWT，有效期 48 小时，没有刷新接口。账号由部署方配置，作业按项目或人员命名。

## 登录

1. `GET /api/user/captchaImage` → `data.verifyCode` 是验证码明文，网页把它画成图片（会话靠 Cookie）。
2. `POST /api/user/login`，请求体：
   - `loginName`：用户名
   - `passWord`：用页面内置的 RSA 公钥加密（JSEncrypt，PKCS#1 v1.5，base64），公钥在 `js/app.*.js` 的模块 `21f2`
   - `verifyCode`：验证码
   - `time`：本机时间 `YYYY-MM-DD HH:MM:SS`
3. 返回 `data.token`，另有 `userId`、`userName`、`nickName`、`childrenList`（菜单）等。

服务器会校验验证码，而且有时效：页面放久了再登录会返回 `{"code": "-1", "msg": "验证码已失效"}`，要重新取。
验证码由用户在监控页上输入，不自动填。

## 找到要续租的作业

`POST /api/workManage/list`，请求体 `{"adminFlag": 1, "pageReq": {"pageNum": 1, "pageSize": 50}, "poolName": "", "taskName": ""}`（看板按名字查时 `taskName` 填作业名）。
`data.workManageVOList` 每行：`id`、`taskName`、`poolName`、`createTime`、`runningCount`、`totalCount`、`unusedTime`（剩余时长，如 `6d 13m 38s`；停止后为“无限期”）。

`GET /api/workManage/getWorkTaskInfo/<id>` → `data.workTaskRoleVOList[]` 每个角色有 `ipAddress`（192.0.2.29）、`portMap`（`{"22": "15155"}`）、
`containerId`、`status`、`remark`（“容器正在运行”）。

共用账号下还有别人的作业，停止前要核对四项都对上：`taskName` 为 ALICE、`runningCount` ≥ 1、`poolName` 为 pool-private、`portMap` 含 22→15155。

## 停止、克隆、提交

1. `POST /api/workManage/stop?id=<id>`（无请求体）→ `code "0"`。之后该行 `runningCount` 变 0，`unusedTime` 变“无限期”。
2. `POST /api/workManage/clone?id=<id>`（无请求体）→ `data` 是原作业的配置，不会新建作业：

   ```json
   {"taskName": "ALICE", "poolId": "…", "machineAmount": 1, "envMap": {}, "minFail": null, "minSuccess": null,
    "cloneStorageVolumeVOList": [{"mountPath": "/root/data1", "perm": "rw", "storagePath": "/sitonholy/nfs/192.0.2.20/nfs/users/example-team"}],
    "cloneTaskRoleVOList": [{"taskRoleName": "taskRole1", "allocatedQuantity": 1, "specificationId": "…",
                             "imageName": "192.0.2.20:8888/example-team/ubuntu:pytorch2.4",
                             "runCmd": "/etc/init.d/ssh restart && sleep infinity",
                             "cloneTaskHostPortDTOList": [{"containerPort": "22", "environmentPortName": "OTHER", "hostPort": "15155"}]}]}
   ```

   向导中间还会调 `getResourcePool/1`、`getResourceSpecification/<poolId>`、`dataManage/volumeByUserList/<userId>`、
   `imageManage/queryArtifactsAndTags` 填下拉框，提交用不到。
3. `POST /api/submitWork/addWorkTask`，请求体由克隆结果改名得到 → `code "0"`：
   - `taskName`、`poolId`、`machineAmount`、`envMap` 原样
   - `storageVolumeAddDTOList` = `cloneStorageVolumeVOList`
   - `taskRoleAddDTOList` = `cloneTaskRoleVOList`，每个角色加 `poolId`，`cloneTaskHostPortDTOList` 改名为 `taskHostPortAddDTOList`
   - 去掉 `minFail`、`minSuccess`
4. 之后 `workManage/list` 里出现新的 ALICE 行，`runningCount` 为 1，`unusedTime` 约 7 天。

停止、克隆与重建耗时由平台决定；以探针读到的新容器启动时间核对租期。

## 换人、开新格用到的接口（照前端代码推的，待真实核对时校正）

下面几项是从平台前端代码（`js/app.*.js` 与各页面 chunk）读出来的，还没在真实请求里逐字段核对过。
看板与平台核对时把规格、存储卷整份存进 `pools.json`，资源池与镜像仓库接口的第一条原样存在 `sample`，对上后再改这里。
字段认不出时看板只提示“没核对上”，不据此拦下提交；认得出而查不到的才拦。

- `GET /api/submitWork/getResourcePool/1` → 资源池列表（`poolId`、`poolName`、所在节点）。
- `GET /api/submitWork/getResourceSpecification/<poolId>` → 规格列表：`specificationId`、`specificationName`、
  `specificationAmount`（总数）、`quantityAvailable`（空几张）、`isSchedule`（能否排队）、`queueNum`。看板不用排队。
- `POST /api/dataManage/volumeByUserList/<userId>`（`userId` 来自登录返回）→ 存储卷列表，`fullPath` 对应克隆配置里的 `storagePath`。
- `POST /api/imageManage/queryArtifactsAndTags`：不带 `repositoriesId` 时列镜像仓库，带了列这个仓库的标签。
- `POST /api/workManage/packagingContainer?id=<containerId>&url=<ipAddress>:<port>&repository=<项目/名字:标签>`：
  把运行中的容器打包推到仓库。看板不调用这个接口。

提交表单的限制（前端校验）：作业名 `^[0-9a-zA-Z-_\~]*$`；`hostPort` 在 10000–20000；
另有“后台生成端口”（autoPort）选项，行为没核对过，看板不用，端口由看板自己挑（见 `docs/fleet.md`）。
