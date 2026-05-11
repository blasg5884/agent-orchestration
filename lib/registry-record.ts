import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as iam from 'aws-cdk-lib/aws-iam';
import {
  AwsCustomResource,
  AwsCustomResourcePolicy,
  PhysicalResourceId,
  PhysicalResourceIdReference,
} from 'aws-cdk-lib/custom-resources';

export interface RegistryRecordProps {
  /**
   * Registry ARN (e.g. arn:aws:bedrock-agentcore:ap-northeast-1:123:registry/xyz).
   * The bedrock-agentcore-control API accepts both the short ID and the full ARN
   * for the `registryId` parameter. Using the ARN is safer because the short ID
   * format returned by createRegistry may not always match the expected alphanumeric
   * pattern validated by the API.
   */
  readonly registryArn: string;
  /** Record name (1–255 chars). */
  readonly name: string;
  readonly description?: string;
  readonly recordVersion: string;
  /**
   * A2A Agent Card JSON (per A2A spec) describing this sub-agent. Must include
   * at minimum `name`, `description`, `version`, `url`, and `capabilities`.
   * The orchestrator reads `url` from this card to dispatch via A2A.
   */
  readonly agentCard: Record<string, unknown>;
  /** If true, immediately submit for approval after creation. */
  readonly submitForApproval?: boolean;
}

/**
 * One AGENT-type record in the Agent Registry. Backed by AwsCustomResource
 * because Agent Registry is preview and has no CloudFormation resource type.
 */
export class RegistryRecord extends Construct {
  public readonly recordId: string;
  public readonly recordArn: string;

  constructor(scope: Construct, id: string, props: RegistryRecordProps) {
    super(scope, id);

    const policy = AwsCustomResourcePolicy.fromStatements([
      new iam.PolicyStatement({
        actions: [
          'bedrock-agentcore:CreateRegistryRecord',
          'bedrock-agentcore:GetRegistryRecord',
          'bedrock-agentcore:DeleteRegistryRecord',
          'bedrock-agentcore:SubmitRegistryRecordForApproval',
          'bedrock-agentcore:UpdateRegistryRecordStatus',
        ],
        resources: ['*'],
      }),
    ]);

    const create = new AwsCustomResource(this, 'Create', {
      onCreate: {
        service: 'bedrock-agentcore-control',
        action: 'createRegistryRecord',
        parameters: {
          registryId: props.registryArn,
          name: props.name,
          description: props.description ?? props.name,
          recordVersion: props.recordVersion,
          descriptorType: 'AGENT',
          descriptors: {
            agent: {
              card: {
                schemaVersion: '0.3',
                inlineContent: JSON.stringify(props.agentCard),
              },
            },
          },
        },
        physicalResourceId: PhysicalResourceId.fromResponse('recordId'),
      },
      onDelete: {
        service: 'bedrock-agentcore-control',
        action: 'deleteRegistryRecord',
        parameters: {
          registryId: props.registryArn,
          recordId: new PhysicalResourceIdReference(),
        },
      },
      policy,
      installLatestAwsSdk: true,
      timeout: cdk.Duration.minutes(5),
    });

    this.recordId = create.getResponseField('recordId');
    this.recordArn = create.getResponseField('recordArn');

    if (props.submitForApproval ?? true) {
      const submit = new AwsCustomResource(this, 'Submit', {
        onCreate: {
          service: 'bedrock-agentcore-control',
          action: 'submitRegistryRecordForApproval',
          parameters: {
            registryId: props.registryArn,
            recordId: this.recordId,
          },
          physicalResourceId: PhysicalResourceId.of(`${this.recordId}-submit`),
        },
        policy,
        installLatestAwsSdk: true,
        timeout: cdk.Duration.minutes(5),
      });
      submit.node.addDependency(create);
    }
  }
}
