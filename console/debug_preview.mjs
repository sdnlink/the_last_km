import {renameSync, writeFileSync} from 'node:fs';
import {createServer, loadEnv} from 'vite';

function report(data) {
  writeFileSync('../result.tmp', JSON.stringify(data), {mode: 0o600});
  renameSync('../result.tmp', '../result.json');
}

function needsBackend(message) {
  report({status: 'needs_input', field: 'backend', message});
}

const mode = 'development';
const env = loadEnv(mode, process.cwd());
const prefix = env.VITE_APP_BASE_API;
const backend = env.VITE_BASEURL;
if (prefix && !backend) {
  needsBackend('项目没有设置远程测试后端。填写地址后，系统会自动启动预览并检查连接。');
} else {
  let server;
  try {
    server = await createServer({
      mode,
      cacheDir: '.auto-monitor-vite-cache',
      server: {host: '127.0.0.1', port: 5173, open: false, strictPort: false},
    });
    await server.listen();
    const url = 'http://127.0.0.1:' + server.httpServer.address().port;
    if (prefix) {
      if (!server.config.server.proxy?.[prefix]) {
        throw new Error('项目开发配置缺少对应的代理规则。');
      }
      try {
        const response = await fetch(url + prefix.replace(/\/$/, '') + '/', {
          method: 'HEAD', redirect: 'manual', signal: AbortSignal.timeout(5000),
        });
        if (response.status >= 500) throw new Error();
      } catch {
        needsBackend('项目配置的测试后端暂时无法连接。请填写可用的后端地址。');
        await server.close();
        process.exit(0);
      }
    }
    report({status: 'ready', url});
    for (const signal of ['SIGINT', 'SIGTERM']) {
      process.once(signal, async () => {
        await server.close();
        process.exit(0);
      });
    }
  } catch (error) {
    console.error(error);
    report({status: 'failed', message: '本地前端启动失败，请核对项目的 Vite 配置。'});
    await server?.close();
    process.exitCode = 1;
  }
}
