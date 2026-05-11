import * as cdk from 'aws-cdk-lib';
import * as path from 'path';
import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cr from 'aws-cdk-lib/custom-resources';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as bedrockagentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import { RegistryRecord } from './registry-record';

export interface AgentsStackProps extends cdk.StackProps {
  readonly registryId: string;
  readonly registryArn: string;
  /** SSM Parameter Store path that holds the registry ARN (set by RegistryStack). */
  readonly registryArnSsmParamName: string;
  /** SSM Parameter Store path that holds the registry short ID (set by RegistryStack). */
  readonly registryIdSsmParamName: string;
  /** SSM Parameter Store path that holds the registry name (set by RegistryStack). */
  readonly registryNameSsmParamName: string;
}

interface SubAgentSpec {
  readonly key: string;
  readonly runtimeName: string;
  readonly dockerDir: string;
  readonly description: string;
  readonly skills: ReadonlyArray<{
    readonly id: string;
    readonly name: string;
    readonly description: string;
    readonly tags: ReadonlyArray<string>;
  }>;
}

const SUBAGENTS: ReadonlyArray<SubAgentSpec> = [
  {
    key: 'weather',
    runtimeName: 'weather_agent',
    dockerDir: 'agents/weather',
    description: '天気検索エージェント。都市名や緯度経度から現在の天気を返します。',
    skills: [
      {
        id: 'get_weather',
        name: 'get_weather',
        description: '指定された場所の現在の天気を取得',
        tags: ['weather', '天気', '気象'],
      },
    ],
  },
  {
    key: 'zipcode',
    runtimeName: 'zipcode_agent',
    dockerDir: 'agents/zipcode',
    description: '郵便番号検索エージェント。日本の7桁の郵便番号から住所を返します。',
    skills: [
      {
        id: 'lookup_zipcode',
        name: 'lookup_zipcode',
        description: '日本の郵便番号から都道府県・市区町村・町域を取得',
        tags: ['zipcode', '郵便番号', '住所'],
      },
    ],
  },
];

/**
 * Provisions:
 *  - One AgentCore Runtime per sub-agent (A2A protocol)
 *  - One AgentCore Runtime for the orchestrator (HTTP protocol)
 *  - One Registry record (AGENT-type) per sub-agent so the orchestrator can
 *    discover them at runtime.
 *  - A shared Lambda Provider for registry-record CRUD (boto3-based, with
 *    full CloudWatch logging of the preview API responses).
 */
export class AgentsStack extends cdk.Stack {
  public readonly orchestratorRuntimeArn: string;

  constructor(scope: Construct, id: string, props: AgentsStackProps) {
    super(scope, id, props);

    const runtimeRole = this.createRuntimeRole();

    // Resolve registry identifier candidates via SSM dynamic references.
    // CloudFormation resolves these before any Lambda invocation.
    const registryIdFromSsm = ssm.StringParameter.valueForStringParameter(
      this,
      props.registryIdSsmParamName,
    );
    const registryArnFromSsm = ssm.StringParameter.valueForStringParameter(
      this,
      props.registryArnSsmParamName,
    );
    const registryNameFromSsm = ssm.StringParameter.valueForStringParameter(
      this,
      props.registryNameSsmParamName,
    );

    // Lambda Provider for registry-record CRUD (boto3, full logging).
    const recordProviderFn = new lambda.Function(this, 'RecordProviderFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      // Bundle the latest boto3 — see RegistryStack for context.
      code: lambda.Code.fromAsset(path.join(process.cwd(), 'lambda/registry_record_provider'), {
        bundling: {
          image: lambda.Runtime.PYTHON_3_13.bundlingImage,
          command: [
            'bash',
            '-c',
            'pip install -r requirements.txt -t /asset-output --no-cache-dir && cp -au . /asset-output',
          ],
        },
      }),
      timeout: cdk.Duration.minutes(10),
      memorySize: 512,
      logRetention: logs.RetentionDays.ONE_WEEK,
    });
    recordProviderFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:CreateRegistryRecord',
          'bedrock-agentcore:DeleteRegistryRecord',
          'bedrock-agentcore:GetRegistryRecord',
          'bedrock-agentcore:UpdateRegistryRecord',
          'bedrock-agentcore:SubmitRegistryRecordForApproval',
          'bedrock-agentcore:UpdateRegistryRecordStatus',
          'bedrock-agentcore:ListRegistryRecords',
          // The Lambda polls get_registry_record to wait for status transitions.
          'bedrock-agentcore:GetRegistry',
        ],
        resources: ['*'],
      }),
    );
    const recordProvider = new cr.Provider(this, 'RecordProvider', {
      onEventHandler: recordProviderFn,
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    // --- Sub-agents (A2A) ---
    const subAgentEndpoints: { spec: SubAgentSpec; runtime: bedrockagentcore.CfnRuntime }[] = [];
    for (const spec of SUBAGENTS) {
      const image = new ecrAssets.DockerImageAsset(this, `${spec.key}Image`, {
        directory: path.join(process.cwd(), spec.dockerDir),
        platform: ecrAssets.Platform.LINUX_ARM64,
      });
      const runtime = new bedrockagentcore.CfnRuntime(this, `${spec.key}Runtime`, {
        agentRuntimeName: spec.runtimeName,
        description: spec.description,
        roleArn: runtimeRole.roleArn,
        protocolConfiguration: 'A2A',
        networkConfiguration: { networkMode: 'PUBLIC' },
        agentRuntimeArtifact: {
          containerConfiguration: {
            containerUri: image.imageUri,
          },
        },
      });
      runtime.addDependency(runtimeRole.node.defaultChild as cdk.CfnResource);
      subAgentEndpoints.push({ spec, runtime });
    }

    // --- Register sub-agents in Agent Registry (AGENT record) ---
    //
    // The createRegistryRecord API requires `registryId` to match
    //   (arn:aws...:registry/[a-zA-Z0-9]{12,16})  |  [a-zA-Z0-9]{12,16}
    //
    // We pass the SHORT ID from SSM (set by the registry provider Lambda from
    // the createRegistry response). If that's unavailable for some reason the
    // Lambda logs include the empty value so we can debug.
    for (const { spec, runtime } of subAgentEndpoints) {
      const agentCard = {
        name: spec.runtimeName,
        description: spec.description,
        version: '1.0.0',
        // The A2A URL used by the orchestrator's A2AClientToolProvider.
        url: runtime.attrAgentRuntimeArn,
        protocolVersion: '0.3',
        capabilities: { streaming: false },
        defaultInputModes: ['text/plain'],
        defaultOutputModes: ['text/plain'],
        skills: spec.skills.map((s) => ({
          id: s.id,
          name: s.name,
          description: s.description,
          tags: s.tags,
        })),
      };
      new RegistryRecord(this, `${spec.key}Record`, {
        providerServiceToken: recordProvider.serviceToken,
        registryId: registryIdFromSsm,
        name: spec.runtimeName,
        description: spec.description,
        recordVersion: '1.0.0',
        agentCard,
        submitForApproval: true,
      });
    }

    // --- Orchestrator (HTTP, not registered in registry) ---
    const orchestratorImage = new ecrAssets.DockerImageAsset(this, 'OrchestratorImage', {
      directory: path.join(process.cwd(), 'agents/orchestrator'),
      platform: ecrAssets.Platform.LINUX_ARM64,
    });
    const orchestrator = new bedrockagentcore.CfnRuntime(this, 'OrchestratorRuntime', {
      agentRuntimeName: 'orchestrator_agent',
      description: 'リクエストを適切なサブエージェントに振り分けるオーケストレーター',
      roleArn: runtimeRole.roleArn,
      protocolConfiguration: 'HTTP',
      networkConfiguration: { networkMode: 'PUBLIC' },
      agentRuntimeArtifact: {
        containerConfiguration: {
          containerUri: orchestratorImage.imageUri,
        },
      },
      environmentVariables: {
        AGENT_REGISTRY_ID: registryIdFromSsm,
        AGENT_REGISTRY_ARN: registryArnFromSsm,
        AGENT_REGISTRY_NAME: registryNameFromSsm,
        // Override the model id without rebuilding the container image.
        // Set ORCHESTRATOR_MODEL_ID in your shell before `cdk deploy` to
        // pin a specific Bedrock model (e.g. one you have access to).
        ORCHESTRATOR_MODEL_ID:
          process.env.ORCHESTRATOR_MODEL_ID ?? 'apac.anthropic.claude-sonnet-4-20250514-v1:0',
      },
    });
    orchestrator.addDependency(runtimeRole.node.defaultChild as cdk.CfnResource);

    this.orchestratorRuntimeArn = orchestrator.attrAgentRuntimeArn;

    new cdk.CfnOutput(this, 'OrchestratorRuntimeArn', { value: this.orchestratorRuntimeArn });
    for (const { spec, runtime } of subAgentEndpoints) {
      new cdk.CfnOutput(this, `${spec.key}RuntimeArn`, { value: runtime.attrAgentRuntimeArn });
    }
  }

  /**
   * Single role shared by all runtimes (prototype). Allows ECR pull, Bedrock model
   * invocation, AgentCore A2A calls, and Registry search.
   *
   * Policies are attached as inlinePolicies (embedded in the Role resource)
   * rather than via addToPolicy() — the latter creates a separate AWS::IAM::Policy
   * resource that the CfnRuntime does not auto-depend on, leading to a race:
   * AgentCore Control validates ECR access at runtime-creation time and fails
   * with "Access denied while validating ECR URI" before the inline policy
   * is attached. Inline policies are part of the role's CFn resource and
   * therefore exist atomically with the role.
   */
  private createRuntimeRole(): iam.Role {
    return new iam.Role(this, 'AgentRuntimeRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': cdk.Aws.ACCOUNT_ID },
          // Use ":*" rather than ":runtime/*" so the trust policy also permits
          // the validation-phase AssumeRole (where no runtime ARN exists yet).
          ArnLike: {
            'aws:SourceArn': `arn:aws:bedrock-agentcore:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:*`,
          },
        },
      }),
      inlinePolicies: {
        EcrPull: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: ['ecr:GetAuthorizationToken'],
              resources: ['*'],
            }),
            new iam.PolicyStatement({
              actions: [
                'ecr:BatchCheckLayerAvailability',
                'ecr:GetDownloadUrlForLayer',
                'ecr:BatchGetImage',
              ],
              resources: ['*'],
            }),
          ],
        }),
        BedrockInvoke: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: [
                'bedrock:InvokeModel',
                'bedrock:InvokeModelWithResponseStream',
                'bedrock:Converse',
                'bedrock:ConverseStream',
              ],
              resources: ['*'],
            }),
          ],
        }),
        AgentCore: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: [
                'bedrock-agentcore:InvokeAgentRuntime',
                'bedrock-agentcore:InvokeAgent',
                // Registry MCP endpoint — required for the orchestrator's
                // semantic search / tool listing path.
                'bedrock-agentcore:InvokeRegistryMcp',
                'bedrock-agentcore:SearchRegistryRecords',
                'bedrock-agentcore:ListRegistryRecords',
                'bedrock-agentcore:GetRegistryRecord',
              ],
              resources: ['*'],
            }),
          ],
        }),
        Logs: new iam.PolicyDocument({
          statements: [
            new iam.PolicyStatement({
              actions: [
                'logs:CreateLogGroup',
                'logs:CreateLogStream',
                'logs:PutLogEvents',
                'logs:DescribeLogStreams',
              ],
              resources: ['*'],
            }),
          ],
        }),
      },
    });
  }
}
