import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';

export interface RegistryRecordProps {
  /** Service token of the shared registry-record Provider Lambda. */
  readonly providerServiceToken: string;
  /**
   * The registry identifier (short ID, name, or ARN). Receives whatever value
   * the createRegistryRecord API accepts as `registryId`. The Lambda passes
   * this through verbatim.
   */
  readonly registryId: string;
  /** Record name (1–255 chars). */
  readonly name: string;
  readonly description?: string;
  readonly recordVersion: string;
  /**
   * A2A Agent Card JSON. Stored in `descriptors.agent.card.inlineContent`.
   * The orchestrator reads `url` from this card to dispatch via A2A.
   */
  readonly agentCard: Record<string, unknown>;
  /** If true, immediately submit for approval after creation. */
  readonly submitForApproval?: boolean;
}

/**
 * One AGENT-type record in the Agent Registry. Backed by our own boto3 Lambda
 * (see lambda/registry_record_provider/index.py) via cdk.CustomResource so that
 * resource properties are passed through CloudFormation's normal resolution
 * pipeline rather than being JSON-encoded inside an AwsCustomResource string.
 */
export class RegistryRecord extends Construct {
  public readonly recordId: string;

  constructor(scope: Construct, id: string, props: RegistryRecordProps) {
    super(scope, id);

    const resource = new cdk.CustomResource(this, 'Resource', {
      serviceToken: props.providerServiceToken,
      properties: {
        registryId: props.registryId,
        name: props.name,
        description: props.description ?? props.name,
        recordVersion: props.recordVersion,
        // Pre-serialise to JSON here so the Lambda receives a single string field —
        // avoids any ambiguity around nested object encoding in custom resource props.
        agentCard: JSON.stringify(props.agentCard),
        submitForApproval: String(props.submitForApproval ?? true),
      },
    });

    this.recordId = resource.getAttString('recordId');
  }
}
