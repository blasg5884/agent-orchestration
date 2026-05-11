import * as cdk from 'aws-cdk-lib';
import * as path from 'path';
import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ecrAssets from 'aws-cdk-lib/aws-ecr-assets';
import * as bedrockagentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import { RegistryRecord } from './registry-record';

export interface AgentsStackProps extends cdk.StackProps {
  readonly registryId: string;
  readonly registryArn: string;
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
 *  - One Registry record (AGENT-type) per sub-agent so the orchestrator
 *    can discover them at runtime.
 */
export class AgentsStack extends cdk.Stack {
  public readonly orchestratorRuntimeArn: string;

  constructor(scope: Construct, id: string, props: AgentsStackProps) {
    super(scope, id, props);

    const runtimeRole = this.createRuntimeRole();

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
        registryId: props.registryId,
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
        AGENT_REGISTRY_ID: props.registryId,
        AGENT_REGISTRY_ARN: props.registryArn,
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
   */
  private createRuntimeRole(): iam.Role {
    const role = new iam.Role(this, 'AgentRuntimeRole', {
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': cdk.Aws.ACCOUNT_ID },
          ArnLike: {
            'aws:SourceArn': `arn:aws:bedrock-agentcore:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:runtime/*`,
          },
        },
      }),
    });
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock:InvokeModel',
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:Converse',
          'bedrock:ConverseStream',
        ],
        resources: ['*'],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:InvokeAgentRuntime',
          'bedrock-agentcore:InvokeAgent',
          'bedrock-agentcore:SearchRegistryRecords',
          'bedrock-agentcore:ListRegistryRecords',
          'bedrock-agentcore:GetRegistryRecord',
        ],
        resources: ['*'],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'ecr:GetAuthorizationToken',
          'ecr:BatchCheckLayerAvailability',
          'ecr:GetDownloadUrlForLayer',
          'ecr:BatchGetImage',
        ],
        resources: ['*'],
      }),
    );
    role.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:CreateLogGroup',
        ],
        resources: ['*'],
      }),
    );
    return role;
  }
}
