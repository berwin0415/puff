import { NestFactory } from '@nestjs/core';
import { AppModule } from './app.module.js';

// Loads apps/api/.env when present. Node's built-in loader keeps the API
// dependency-free; missing files are ignored so real env vars still apply.
try {
  process.loadEnvFile();
} catch {
  // No .env file next to the API package: nothing to load.
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
