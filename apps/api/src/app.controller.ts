import { Controller, Get } from '@nestjs/common';
import type { HealthStatus } from './app.service.js';
import { AppService } from './app.service.js';

@Controller()
export class AppController {
  constructor(private readonly appService: AppService) {}

  @Get()
  getRoot(): { name: string; version: string } {
    return {
      name: 'puff-api',
      version: process.env.npm_package_version ?? '0.0.0',
    };
  }

  @Get('health')
  getHealth(): HealthStatus {
    return this.appService.getHealth();
  }
}
