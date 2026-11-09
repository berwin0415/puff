import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module.js';

// 存在 domains/puff/services/api/.env 时加载它。使用 Node 内置加载器让 API
// 不依赖额外包；文件不存在时忽略，真实环境变量仍然生效。
try {
  process.loadEnvFile();
} catch {
  // API 包旁边没有 .env 文件：无需加载。
}

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  app.setGlobalPrefix('api');
  app.enableCors();

  const port = Number(process.env.PORT ?? 3000);
  await app.listen(port);

  console.log(`API ready on http://localhost:${port}/api`);
}

await bootstrap();
