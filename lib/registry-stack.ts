import * as cdk from 'aws-cdk-lib';
import * as path from 'path';
import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as cr from 'aws-cdk-lib/custom-resources';

export interface RegistryStackProps extends cdk.StackProps {
  readonly registryName: string;
  readonly description?: string;
  /**
   * Auto-approve newly submitted records. When false, records stay in
   * PENDING_APPROVAL and need manual approval via console/CLI.
   */
  readonly autoApprove?: boolean;
}

/**
 * AgentCore Agent Registry is in preview and has no CloudFormation resource type.
 *
 * Provisioned via our own boto3-based Lambda (NOT CDK's AwsCustomResource),
 * because the preview API's response handling and field-name conventions are
 * easier to debug end-to-end when the Lambda is under our direct control.
 *
 * Cross-stack value passing uses SSM Parameter Store so CloudFormation resolves
 * the ARN before invoking any downstream Lambda.
 */
export class RegistryStack extends cdk.Stack {
  public readonly registryId: string;
  public readonly registryArn: string;
  public readonly autoApprove: boolean;
  /** SSM Parameter Store path that holds the registry ARN. */
  public readonly registryArnSsmParamName: string;
  /** SSM Parameter Store path that holds the registry name (stable, known up-front). */
  public readonly registryNameSsmParamName: string;
  /** SSM path that holds whichever value works as the registryId — id, then arn, then name. */
  public readonly registryIdSsmParamName: string;

  constructor(scope: Construct, id: string, props: RegistryStackProps) {
    super(scope, id, props);

    this.autoApprove = props.autoApprove ?? true;

    const providerFn = new lambda.Function(this, 'RegistryProviderFn', {
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(process.cwd(), 'lambda/registry_provider')),
      timeout: cdk.Duration.minutes(10),
      memorySize: 256,
      logRetention: logs.RetentionDays.ONE_WEEK,
    });
    providerFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:CreateRegistry',
          'bedrock-agentcore:DeleteRegistry',
          'bedrock-agentcore:UpdateRegistry',
          'bedrock-agentcore:GetRegistry',
          'bedrock-agentcore:ListRegistries',
        ],
        resources: ['*'],
      }),
    );

    const provider = new cr.Provider(this, 'RegistryProvider', {
      onEventHandler: providerFn,
      logRetention: logs.RetentionDays.ONE_WEEK,
    });

    const registry = new cdk.CustomResource(this, 'Registry', {
      serviceToken: provider.serviceToken,
      properties: {
        name: props.registryName,
        description: props.description ?? 'Agent Registry for the orchestration prototype',
        autoApprove: String(this.autoApprove),
      },
    });

    this.registryId = registry.getAttString('registryId');
    this.registryArn = registry.getAttString('registryArn');

    // SSM params — always-defined values for cross-stack reference.
    // SSM dynamic refs are resolved by CloudFormation before any Lambda invocation,
    // sidestepping the issues seen with CFn token strings inside AwsCustomResource params.
    this.registryArnSsmParamName = '/agent-orchestration/registry-arn';
    new ssm.StringParameter(this, 'RegistryArnParam', {
      parameterName: this.registryArnSsmParamName,
      stringValue: this.registryArn,
      description: 'AgentCore Agent Registry ARN',
    });

    this.registryNameSsmParamName = '/agent-orchestration/registry-name';
    new ssm.StringParameter(this, 'RegistryNameParam', {
      parameterName: this.registryNameSsmParamName,
      stringValue: props.registryName,
      description: 'AgentCore Agent Registry name (deterministic, useful when ID/ARN are absent)',
    });

    // Preferred identifier for downstream API calls (registryId param).
    // We store the actual short ID returned by createRegistry here; downstream
    // consumers read this via SSM dynamic reference.
    this.registryIdSsmParamName = '/agent-orchestration/registry-id';
    new ssm.StringParameter(this, 'RegistryIdParam', {
      parameterName: this.registryIdSsmParamName,
      stringValue: this.registryId,
      description: 'AgentCore Agent Registry short ID (12-16 alphanumeric)',
    });

    new cdk.CfnOutput(this, 'RegistryId', { value: this.registryId });
    new cdk.CfnOutput(this, 'RegistryArn', { value: this.registryArn });
    new cdk.CfnOutput(this, 'RegistryName', { value: props.registryName });
  }
}
