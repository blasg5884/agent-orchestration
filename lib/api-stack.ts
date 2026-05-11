import * as cdk from 'aws-cdk-lib';
import * as path from 'path';
import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigw from 'aws-cdk-lib/aws-apigateway';

export interface ApiStackProps extends cdk.StackProps {
  readonly registryId: string;
  readonly registryArn: string;
  readonly orchestratorRuntimeArn: string;
}

/**
 * REST API:
 *   GET  /v1/agents          → list_agents Lambda
 *   GET  /v1/agents?name=... → list_agents Lambda (filtered)
 *   POST /v1/invoke          → invoke Lambda → orchestrator runtime
 */
export class ApiStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    // Both Lambdas bundle the latest boto3 because the Python 3.13 runtime ships
    // a boto3 that lacks the Bedrock AgentCore preview methods (list_registry_records,
    // search_registry_records, invoke_agent_runtime, ...).
    const bundling: cdk.BundlingOptions = {
      image: lambda.Runtime.PYTHON_3_13.bundlingImage,
      command: [
        'bash',
        '-c',
        'pip install -r requirements.txt -t /asset-output --no-cache-dir && cp -au . /asset-output',
      ],
    };

    const listAgentsFn = new lambda.Function(this, 'ListAgentsFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(process.cwd(), 'lambda/list_agents'), { bundling }),
      timeout: cdk.Duration.seconds(30),
      memorySize: 512,
      environment: {
        AGENT_REGISTRY_ID: props.registryId,
        AGENT_REGISTRY_ARN: props.registryArn,
      },
    });
    listAgentsFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:ListRegistryRecords',
          'bedrock-agentcore:GetRegistryRecord',
          'bedrock-agentcore:SearchRegistryRecords',
        ],
        resources: ['*'],
      }),
    );

    const invokeFn = new lambda.Function(this, 'InvokeFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(process.cwd(), 'lambda/invoke'), { bundling }),
      timeout: cdk.Duration.minutes(5),
      memorySize: 512,
      environment: {
        ORCHESTRATOR_RUNTIME_ARN: props.orchestratorRuntimeArn,
      },
    });
    invokeFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock-agentcore:InvokeAgentRuntime'],
        resources: [props.orchestratorRuntimeArn, `${props.orchestratorRuntimeArn}/*`],
      }),
    );

    const api = new apigw.RestApi(this, 'Api', {
      restApiName: 'agent-orchestration-api',
      deployOptions: {
        stageName: 'prod',
        tracingEnabled: true,
      },
      defaultCorsPreflightOptions: {
        allowOrigins: apigw.Cors.ALL_ORIGINS,
        allowMethods: ['GET', 'POST', 'OPTIONS'],
        allowHeaders: ['Content-Type', 'Authorization'],
      },
    });

    const v1 = api.root.addResource('v1');
    const agents = v1.addResource('agents');
    agents.addMethod('GET', new apigw.LambdaIntegration(listAgentsFn));
    const invoke = v1.addResource('invoke');
    invoke.addMethod('POST', new apigw.LambdaIntegration(invokeFn));

    new cdk.CfnOutput(this, 'ApiUrl', { value: api.url });
  }
}
