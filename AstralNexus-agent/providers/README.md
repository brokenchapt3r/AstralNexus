# AstralNexus 模型提供者

## 概述

AstralNexus 支持多种模型提供者，包括 OpenAI、Anthropic 等。

## 支持的提供者

1. **OpenAI** - 支持 GPT 系列模型
2. **Anthropic** - 支持 Claude 系列模型
3. **自定义提供者** - 支持自定义模型提供者

## 配置提供者

在 `config.yaml` 文件中配置模型提供者：

```yaml
model:
  default: "gpt-4"
  providers:
    - name: "openai"
      api_key: "${OPENAI_API_KEY}"
    - name: "anthropic"
      api_key: "${ANTHROPIC_API_KEY}"
```

## 添加新提供者

要添加新的模型提供者，请按照以下步骤操作：

1. 在 `providers/` 目录下创建新的 Python 文件
2. 实现提供者接口
3. 在配置文件中注册提供者