#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { RegistryStack } from '../lib/registry-stack';
import { AgentsStack } from '../lib/agents-stack';
import { ApiStack } from '../lib/api-stack';

const app = new cdk.App();

const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION ?? 'ap-northeast-1',
};

// 1. AgentCore Agent Registry (preview API via Custom Resource)
const registryStack = new RegistryStack(app, 'AgentRegistryStack', {
  env,
  registryName: 'AgentRegistry',
});

// 2. AgentCore Runtimes: orchestrator + 2 sub-agents (weather, zipcode)
const agentsStack = new AgentsStack(app, 'AgentOrchestrationAgentsStack', {
  env,
  registryId: registryStack.registryId,
  registryArn: registryStack.registryArn,
  registryArnSsmParamName: registryStack.registryArnSsmParamName,
  registryIdSsmParamName: registryStack.registryIdSsmParamName,
  registryNameSsmParamName: registryStack.registryNameSsmParamName,
});
agentsStack.addDependency(registryStack);

// 3. API Gateway + Lambda
new ApiStack(app, 'AgentOrchestrationApiStack', {
  env,
  registryId: registryStack.registryId,
  registryArn: registryStack.registryArn,
  orchestratorRuntimeArn: agentsStack.orchestratorRuntimeArn,
});
