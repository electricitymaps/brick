# brick 🧱

`brick` is a tool used to build and deploy monorepos, and is internally used at [Tomorrow](https://www.tmrow.com).
It uses docker (buildkit) as the build engine.

## Installation
Make sure you have docker installed, and then run:
```
python3 setup.py install
```

## Usage
First, create an empty WORKSPACE file at the root of your repo:
```
touch WORKSPACE
```

Then, for each folder that you'd like to build/deploy, you can create a BUILD.yaml file that describes the dependencies and the build/deploy steps.
Each step is cached and will only re-run if the commands change, or if the input change.

This is an example of the configuration used to build the Tomorrow website to Github Pages:

```yaml
name: www

steps:

  prepare:
    image: node:10.3  # This is a docker image that will be used (optional)
    commands:
      - yarn
    inputs:
      - package.json
      - yarn.lock

  build:
    commands:
      - yarn lint
      - yarn build
    inputs:
      - static
      - src
      - gatsby-*.js
      - .eslintrc.js
    outputs:
      - public

  deploy:
    commands:
      - mkdir -p -m 0700 ~/.ssh && ssh-keyscan github.com >> ~/.ssh/known_hosts
      - --mount=type=ssh git clone --depth 1 git@github.com:tmrowco/tmrowapp.git -b gh-pages
      - rm -rf ./tmrowapp/* && cp -r ../www/public/* ./tmrowapp
      - echo 'www.tmrow.com' > tmrowapp/CNAME
      - cd tmrowapp &&
        git config user.name "brick" &&
        git config user.email "brick@tmrow.com" &&
        git add . &&
        git commit -m "deploy" --allow-empty &&
        git push
    pass_ssh: True
```

Note the deployment will only be triggered if any file declared as inputs changes, or if the deployment commands change.
