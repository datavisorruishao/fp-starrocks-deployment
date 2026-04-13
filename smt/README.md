# FeatureResolverTransform SMT

Kafka Connect SMT that resolves integer feature IDs in the Kafka `featureMap` field into named,
typed columns by querying FP MySQL. Used by the Iceberg sink connector in `charts-iceberg`.

## Build

```bash
mvn package -DskipTests
# produces: target/fp-feature-smt-{version}.jar
```

Requires Java 17 and Maven.

## Upload to S3

After building, upload the JAR to each environment's S3 bucket:

```bash
VERSION=1.1.0

# dev (us-west-2)
aws s3 cp target/fp-feature-smt-1.0.0.jar \
  s3://datavisor-dev-us-west-2-iceberg/cre-6630/smt/fp-feature-smt-${VERSION}.jar

# preprod (us-west-2)
aws s3 cp target/fp-feature-smt-1.0.0.jar \
  s3://datavisor-preprod-us-west-2-iceberg/cre-6630/smt/fp-feature-smt-${VERSION}.jar

# preprod (us-east-1)
aws s3 cp target/fp-feature-smt-1.0.0.jar \
  s3://datavisor-preprod-us-east-1-iceberg/cre-6630/smt/fp-feature-smt-${VERSION}.jar
```

Then update `smtJarS3Uri` in each environment's `charts-iceberg/{env}/values.yaml` and redeploy:

```bash
helm upgrade --install fp-iceberg ./charts-iceberg \
  -f charts-iceberg/{env}/values.yaml \
  -n {namespace}
```

Kafka Connect will pull the new JAR on pod restart.
