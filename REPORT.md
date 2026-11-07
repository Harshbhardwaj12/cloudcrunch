# Serverless Image Processing System using AWS Lambda and Amazon S3

**Course:** Cloud Computing Programming — Module 4 Project  **Author:** ____________  **Date:** ____________

## 1. Problem and objective
Images uploaded from phones and cameras are often several megabytes, which wastes
storage and bandwidth and slows down web pages. Processing them on a traditional
server means provisioning, patching and paying for a machine that is idle most of
the time. The objective of this project is to build a system that **automatically
resizes, compresses and converts uploaded images in the cloud without managing any
server**, using an event-driven serverless architecture.

## 2. System architecture
The browser requests a short-lived **presigned POST** from an API (Amazon API Gateway
+ Lambda) and uploads the image directly to an **S3 input bucket**. The upload emits
an `s3:ObjectCreated` event that **invokes the processing Lambda** (Python 3.12 +
Pillow, packaged as a Lambda Layer). The function reads the user's options from the
object's metadata, resizes the image while preserving aspect ratio, compresses it and
converts it to WebP/JPEG/PNG, then writes the result and a JSON summary to a separate
**S3 output bucket**. The frontend polls a status endpoint and displays a
before/after comparison, size statistics and a download link (presigned GET).
The entire infrastructure — buckets, functions, IAM roles, API, log groups and a
CloudWatch error alarm — is defined as code in an AWS SAM template and deployed with
one command.

## 3. Cloud computing concepts demonstrated
| Concept | Implementation |
|---|---|
| Serverless computing | Two Lambda functions; no EC2 instances or OS to manage |
| Event-driven programming | S3 upload event triggers processing automatically |
| Cloud object storage | Separate S3 input/output buckets with lifecycle expiry |
| Elastic scalability | Each upload runs in its own Lambda invocation, concurrently |
| Pay-per-use | Billed per request and per ms of compute; idle cost ≈ 0 |
| Security | Least-privilege IAM, private buckets, presigned URLs, size/type limits signed into the upload policy |
| Infrastructure as Code | `template.yaml` (AWS SAM / CloudFormation) |
| Monitoring | CloudWatch Logs (7-day retention) + Errors alarm |

## 4. Implementation highlights
- **Loop prevention:** the output bucket has no trigger, and the input trigger is
  filtered to the `uploads/` prefix, avoiding recursive invocations.
- **Robust processing:** EXIF rotation correction, transparency flattening for JPEG,
  no upscaling of small images, and errors written to the result file so the user
  sees a clear message instead of an endless wait.
- **Testing:** 10 automated tests (pytest + moto) run the full upload → event →
  process → status flow against a mocked S3, without an AWS account.

## 5. Results
In testing, a 3000×2000 high-quality JPEG (4.8 MB) was reduced to a 1024×683 WebP of
a few KB–hundreds of KB depending on content — typically a **90–99 % size
reduction** — with Lambda execution times of a few hundred milliseconds.
*(Replace with your own measurements from the app's session table / CloudWatch.)*

## 6. Limitations and future work
Polling could be replaced with WebSockets or S3 → SNS notifications; Amazon
Cognito could add user accounts; CloudFront could serve processed images globally;
and multiple renditions (thumbnail, medium, large) could be generated per upload.

## 7. Conclusion
The project shows that a useful media-processing pipeline can be built entirely
from managed services. Compute exists only while an image is being processed, the
system scales automatically with demand, and the whole environment can be created
and destroyed reproducibly from a single template.
