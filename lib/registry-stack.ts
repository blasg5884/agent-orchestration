import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
  PhysicalResourceIdReference,
} from 'aws-cdk-lib/custom-resources';

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
 * Provisioned via boto3 (bedrock-agentcore-control) using a CDK Custom Resource.
 *
 * Records pointing to each runtime are created in AgentsStack (post-runtime ARN).
 */
export class RegistryStack extends cdk.Stack {
  public readonly registryId: string;
  public readonly registryArn: string;
  public readonly autoApprove: boolean;
  /**
   * SSM Parameter Store path that holds the registry ARN.
   * Pass this to AgentsStack so it can resolve the ARN via SSM dynamic reference,
   * avoiding cross-stack CFn token issues inside AwsCustomResource parameters.
   */
  public readonly registryArnSsmParamName: string;

  constructor(scope: Construct, id: string, props: RegistryStackProps) {
    super(scope, id, props);

    this.autoApprove = props.autoApprove ?? true;

    const policy = AwsCustomResourcePolicy.fromStatements([
      new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:CreateRegistry',
          'bedrock-agentcore:GetRegistry',
          'bedrock-agentcore:DeleteRegistry',
          'bedrock-agentcore:ListRegistries',
        ],
        resources: ['*'],
      }),
    ]);

    const createParams = {
      name: props.registryName,
      description: props.description ?? 'Agent Registry for the orchestration prototype',
      searchApiAuthorization: { authType: 'IAM' },
      recordApprovalConfiguration: {
        autoApprovalEnabled: this.autoApprove,
      },
    };

    const registry = new AwsCustomResource(this, 'Registry', {
      onCreate: {
        service: 'bedrock-agentcore-control',
        action: 'createRegistry',
        parameters: createParams,
        physicalResourceId: PhysicalResourceId.fromResponse('registryId'),
      },
      // Registry properties (name, authType) require replacement; updates
      // are not supported in this prototype. Re-create via redeploy if needed.
      onDelete: {
        service: 'bedrock-agentcore-control',
        action: 'deleteRegistry',
        parameters: {
          registryId: new PhysicalResourceIdReference(),
        },
      },
      policy,
      installLatestAwsSdk: true,
      timeout: cdk.Duration.minutes(5),
    });

    this.registryId = registry.getResponseField('registryId');
    this.registryArn = registry.getResponseField('registryArn');

    // Store the registry ARN in SSM so AgentsStack can retrieve it via SSM dynamic
    // reference ({{resolve:ssm:/...}}), which CloudFormation resolves before invoking
    // any Lambda — avoiding CDK cross-stack token issues inside AwsCustomResource params.
    this.registryArnSsmParamName = '/agent-orchestration/registry-arn';
    new ssm.StringParameter(this, 'RegistryArnParam', {
      parameterName: this.registryArnSsmParamName,
      stringValue: this.registryArn,
      description: 'AgentCore Agent Registry ARN for cross-stack reference',
    });

    new cdk.CfnOutput(this, 'RegistryId', { value: this.registryId });
    new cdk.CfnOutput(this, 'RegistryArn', { value: this.registryArn });
  }
}
