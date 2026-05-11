import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
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

    new cdk.CfnOutput(this, 'RegistryId', { value: this.registryId });
    new cdk.CfnOutput(this, 'RegistryArn', { value: this.registryArn });
  }
}
