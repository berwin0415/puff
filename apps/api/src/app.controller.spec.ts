import { Test, TestingModule } from '@nestjs/testing';
import { AppController } from './app.controller.js';
import { AppService } from './app.service.js';

describe('AppController', () => {
  let appController: AppController;

  beforeEach(async () => {
    const app: TestingModule = await Test.createTestingModule({
      controllers: [AppController],
      providers: [AppService],
    }).compile();

    appController = app.get<AppController>(AppController);
  });

  it('identifies the service on the root route', () => {
    expect(appController.getRoot().name).toBe('puff-api');
  });

  it('reports a healthy status', () => {
    const health = appController.getHealth();

    expect(health.status).toBe('ok');
    expect(health.service).toBe('puff-api');
    expect(typeof health.uptime).toBe('number');
  });
});
