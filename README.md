# Readme
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2FCoHDI%2Fcohdi-ci-mock.svg?type=shield)](https://app.fossa.com/projects/git%2Bgithub.com%2FCoHDI%2Fcohdi-ci-mock?ref=badge_shield)


This document explains how to start a mock server for the CI environment to test CoHDI.

## 1. Building the Mock

In this procedure, you will work in a directory named mock, created directly under your home directory.

```bash
mkdir -p ~/mock
```

### 1.1 Cloning the Repository

```bash
cd ~/mock
git clone <URL of the Mock repository>
ls ~/mock
```

The structure should be as follows:

```
Dockerfile  LICENSE  Makefile  Readme.md  app.py  config  requirements.txt
```

### 1.2 Certificate Creation

This step generates a server certificate and private key for HTTPS communication with the mock server.

#### 1.2.1 Create the certs Folder:

```bash
mkdir -p ~/mock/certs
cd ~/mock/certs
```

#### 1.2.2 Create the OpenSSL Config File (openssl.cnf):

```bash
vi openssl.cnf
```

Enter the following content:

```ini
[ req ]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn

[ dn ]
C  = Default Country
L  = Default City
O  = Default Company Ltd
CN = cdimgr.localdomain

[ v3_ca ]
basicConstraints = critical, CA:true
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid:always,issuer

[ v3_req ]
basicConstraints = CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
```

#### 1.2.3 Create the CA Private Key and Certificate:

```bash
openssl req -x509 -newkey rsa:2048 -days 365 -keyout ca.key -out ca.crt   -config openssl.cnf -extensions v3_ca -nodes
```

#### 1.2.4 Create Server Private Key and CSR:

```bash
openssl req -new -nodes -newkey rsa:2048   -keyout server.key -out server.csr   -config openssl.cnf -extensions v3_req
```

#### 1.2.5 Create Server Certificate Signed by CA:

```bash
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial   -out server.crt -days 3650 -sha256 -extensions v3_req -extfile openssl.cnf
```

#### 1.2.6 Verify the Output:

```bash
ls ~/mock/certs
```

The structure should be as follows:

```
ca.crt  ca.key  ca.srl  openssl.cnf  server.crt  server.csr  server.key
```

### 1.3 make：

```bash
cd ~/mock
make
```

After executing the command, a container image named `mock_cohdi:test` will be created.

#### 1.3.1 Verify the Container Image:

```bash
docker images | grep mock_cohdi
```

The output should be as follows:

```
mock_cohdi    test       <image UUID>   23 hours ago   133MB
```

---

## 2. Starting the Mock

Start a container named `mock_cohdi:test`.

Here's an example for launching it using Kubernetes (includes pod and service definitions).

### 2.1 Using the Image in Kubernetes

When Kubernetes uses containerd as its runtime, you cannot directly use images built with Docker.

In that case, please import the image into containerd using the following commands:

```bash
docker save mock_cohdi:test -o mock_cohdi.tar
sudo ctr -n k8s.io images import mock_cohdi.tar
```

After running the above commands, verify that the image has been imported successfully with:

```bash
sudo crictl images | grep mock_cohdi
```

The output should be as follows:

```
docker.io/library/mock_cohdi              test                <image UUID>       206MB
```

### 2.2 Create Pod Definition File (mock-server.yaml):

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: mock-server
  labels:
    app: mock-server
spec:
  containers:
  - name: mock-server
    image: mock_cohdi:test
    ports:
    - containerPort: 443
---
apiVersion: v1
kind: Service
metadata:
  name: mock-server
spec:
  selector:
    app: mock-server
  type: ClusterIP
  ports:
  - protocol: TCP
    port: 443
    targetPort: 443
```

### 2.3 Launch the Pod with the Following Command:

```bash
kubectl apply -f mock-server.yaml
```

### 2.4 Check Pod Status:

```bash
kubectl get pods -A | grep mock
```

If the status is `Running`, the pod has successfully launched.

---

## 3.CoHDI Deployment

Please refer to the Helm-based installation method: https://github.com/CoHDI/cohdi-chart

In the CoHDI configuration, please set the parameters for the CDI management software's ENDPOINT and certificate to point to the mock server's IP address and certificate.



## License
[![FOSSA Status](https://app.fossa.com/api/projects/git%2Bgithub.com%2FCoHDI%2Fcohdi-ci-mock.svg?type=large)](https://app.fossa.com/projects/git%2Bgithub.com%2FCoHDI%2Fcohdi-ci-mock?ref=badge_large)