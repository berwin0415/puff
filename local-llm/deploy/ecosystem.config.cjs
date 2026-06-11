module.exports = {
  apps: [
    {
      name: "local-llm",
      script: "dist/server.js",
      cwd: __dirname + "/..",
      interpreter: "node",
      autorestart: true,
      max_restarts: 10,
      min_uptime: "5s",
      out_file: "logs/local-llm.out.log",
      error_file: "logs/local-llm.err.log",
      log_date_format: "YYYY-MM-DD HH:mm:ss Z",
      env: {
        NODE_ENV: "production",
      },
    },
  ],
};
